"""Провайдеры ИИ: Claude Code на хосте, API Anthropic и OpenAI; модели и ключи администратора."""


import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

import httpx
import psycopg

from zakup import ai_config
from zakup.research.prompts import (
    RESULT_SCHEMA,
)

logger = logging.getLogger(__name__)


PROVIDERS = {
    "claude_code": "Claude Code на сервере (по подписке)",
    "anthropic": "Anthropic API (свой ключ)",
    "openai": "OpenAI API (свой ключ)",
}


DEFAULT_MODELS = {"claude_code": "sonnet", "anthropic": "claude-sonnet-5-5", "openai": "gpt-5"}


# Список в настройках, пока ключа нет; с ключом — список подтягивается у провайдера.
MODELS = {
    "anthropic": [("claude-sonnet-5-5", "Claude Sonnet 5.5 — оптимально"),
                  ("claude-opus-5-5", "Claude Opus 5.5 — точнее, дороже"),
                  ("claude-haiku-5-5", "Claude Haiku 5.5 — быстрее, дешевле")],
    "openai": [("gpt-5", "GPT-5"), ("gpt-5-mini", "GPT-5 mini — дешевле")],
}


def provider_models(provider: str, key: str) -> list[tuple[str, str]]:
    """Модели, доступные по ключу: GET /v1/models у Anthropic или OpenAI."""
    with httpx.Client(timeout=httpx.Timeout(15, connect=5)) as http:
        if provider == "anthropic":
            response = http.get("https://api.anthropic.com/v1/models", params={"limit": 100},
                                headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            response.raise_for_status()
            return [(m["id"], m.get("display_name") or m["id"]) for m in response.json().get("data", [])]
        response = http.get("https://api.openai.com/v1/models",
                            headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        ids = sorted((m["id"] for m in response.json().get("data", [])
                      if m["id"].startswith(("gpt-", "o")) and "audio" not in m["id"]
                      and "realtime" not in m["id"] and "tts" not in m["id"]), reverse=True)
        return [(i, i) for i in ids]


CLAUDE_TIMEOUT = 15 * 60


SUMMARY_MODELS = {"claude_code": "haiku", "anthropic": "claude-haiku-5-5", "openai": "gpt-5-mini"}


class ResearchError(Exception):
    pass


# Текст спецификации и страниц чужой: не даём ему увести WebFetch на сам сервер.
_LOCAL_DENY = {"permissions": {"deny": [
    f"WebFetch(domain:{host})" for host in (
        "localhost", "127.0.0.1", "0.0.0.0", "[::1]", "161.97.156.44",
        "sarsembai.com", "*.sarsembai.com", "host.docker.internal",
        # Имена, которые указывают на сам компьютер.
        "localtest.me", "*.localtest.me", "*.nip.io", "*.sslip.io", "*.localhost",
    )
]}}


_last_usage: dict[str, Any] = {}
_ENV_KEEP = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TERM", "XDG_RUNTIME_DIR"}


def _claude_code(prompt: str, model: str, schema: dict | None = None,
                 web: bool = True) -> dict[str, Any]:
    binary = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    with tempfile.TemporaryDirectory(prefix="zakup-research-") as workdir:
        # Только веб-поиск и чтение страниц: ни файлов, ни команд.
        tools = "WebSearch,WebFetch" if web else ""
        command = [
            binary, "-p", prompt, "--model", model, "--output-format", "json",
            # Только перечисленные инструменты, файлы — только во временной папке.
            "--restricted", "--tools", tools, "--allowedTools", tools,
            "--disallowedTools", "Bash Edit Write Read Glob Grep Task NotebookEdit"
            + ("" if web else " WebSearch WebFetch"),
            "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
            "--settings", json.dumps(_LOCAL_DENY),
            "--max-turns", "60" if web else "3", "--json-schema", json.dumps(schema or RESULT_SCHEMA),
        ]
        try:
            # Пароль базы и ключ шифрования нужны службе, а не Claude Code.
            env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
            done = subprocess.run(command, cwd=workdir, capture_output=True, text=True,
                                  timeout=CLAUDE_TIMEOUT, env=env)
        except subprocess.TimeoutExpired:
            raise ResearchError("Claude не уложился в 15 минут") from None
    try:
        output = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise ResearchError(f"Claude Code вернул не JSON: {done.stderr[-300:] or done.stdout[-300:]}") from None
    if output.get("is_error") or not output.get("structured_output"):
        raise ResearchError(f"Claude Code: {str(output.get('result') or output.get('subtype'))[:300]}")
    # Во сколько обошёлся бы запуск по API: считаем себестоимость запроса для тарифов.
    _last_usage.update(cost_usd=output.get("total_cost_usd"), usage=output.get("usage"),
                       models=output.get("modelUsage"), duration_ms=output.get("duration_ms"),
                       turns=output.get("num_turns"))
    return output["structured_output"]


def _json_from_text(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ResearchError("модель не вернула JSON")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise ResearchError(f"модель вернула битый JSON: {error}") from None


def _anthropic(prompt: str, model: str, key: str, schema: dict | None = None,
               web: bool = True) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": prompt + "\n\nОтветь только JSON по схеме:\n" + json.dumps(schema or RESULT_SCHEMA),
    }]
    with httpx.Client(timeout=httpx.Timeout(300, connect=10)) as http:
        for _ in range(6):
            response = http.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": 16000, "messages": messages,
                      **({"tools": [{"type": "web_search_20250305", "name": "web_search",
                                     "max_uses": 15}]} if web else {})},
            )
            if response.status_code != 200:
                raise ResearchError(f"Anthropic API: HTTP {response.status_code} {response.text[:300]}")
            data = response.json()
            # Длинный поиск сервер отдаёт частями: продолжаем, пока не закончит.
            if data.get("stop_reason") == "pause_turn":
                messages = messages[:1] + [{"role": "assistant", "content": data["content"]}]
                continue
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return _json_from_text(text)
    raise ResearchError("Anthropic API: поиск не закончился")


def _openai(prompt: str, model: str, key: str, schema: dict | None = None,
            web: bool = True) -> dict[str, Any]:
    with httpx.Client(timeout=httpx.Timeout(600, connect=10)) as http:
        response = http.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": prompt,
                  **({"tools": [{"type": "web_search"}]} if web else {}),
                  "text": {"format": {"type": "json_schema", "name": "lot_research",
                                      "schema": schema or RESULT_SCHEMA, "strict": False}}},
        )
    if response.status_code != 200:
        raise ResearchError(f"OpenAI API: HTTP {response.status_code} {response.text[:300]}")
    text = "".join(
        part.get("text", "")
        for item in response.json().get("output", []) if item.get("type") == "message"
        for part in item.get("content", [])
    )
    return _json_from_text(text)


def _light_json(conn: psycopg.Connection, prompt: str, schema: dict) -> dict[str, Any]:
    """Короткий запрос к модели для коротких задач (без интернета)."""
    provider, model = light_model(conn)
    key = ai_config.admin_key(conn, provider) if provider != "claude_code" else None
    # Ответа модели ждём без открытой транзакции: она держала бы блокировки (DeadlockDetected).
    conn.commit()
    if provider == "claude_code":
        return _claude_code(prompt, model, schema, web=False)
    if not key:
        raise ResearchError("в настройках не указан API-ключ")
    runner = _anthropic if provider == "anthropic" else _openai
    return runner(prompt, model, key, schema=schema, web=False)


def api_key(conn: psycopg.Connection, provider: str) -> str | None:
    """API-ключ администратора для провайдера; у Claude Code ключа нет (None)."""
    if provider == "claude_code":
        return None
    key = ai_config.admin_key(conn, provider)
    if not key:
        raise ResearchError("в настройках не указан API-ключ")
    return key


def light_model(conn: psycopg.Connection) -> tuple[str, str]:
    """(провайдер, модель) для коротких задач без интернета: выжимка, вопросы поставщику.
    Модель выбирает администратор отдельно от модели поиска товаров."""
    provider, _, model = ai_config.models(conn)
    if provider == "claude_code" or not model:
        model = SUMMARY_MODELS[provider]
    return provider, model
