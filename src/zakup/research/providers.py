"""Провайдеры ИИ: Claude Code на хосте, API Anthropic и OpenAI; модели и ключи администратора."""


import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
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


class _Usage(threading.local):
    """Стоимость последнего запуска — своя у каждого потока службы (поиски идут параллельно)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


_usage = _Usage()


def last_usage() -> dict[str, Any]:
    return _usage.data


def reset_usage() -> None:
    _usage.data = {}
_ENV_KEEP = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TERM", "XDG_RUNTIME_DIR"}


def _stream_items(text: str) -> list[dict[str, Any]]:
    """Строки «ITEM {json}» из текста модели — предложения, найденные по ходу поиска."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("ITEM "):
            continue
        try:
            item = json.loads(line[5:])
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


REPORT_TOOL = "mcp__zakup__report_offer"
REPORT_SERVER = os.path.join(os.path.dirname(__file__), "report_mcp.py")


def _step(block: dict[str, Any]) -> str | None:
    """Что ИИ делает сейчас — по вызову инструмента: строка для страницы лота."""
    data = block.get("input") or {}
    if block.get("name") == "WebSearch" and data.get("query"):
        return "Ищу: " + str(data["query"])[:80]
    if block.get("name") == "WebFetch" and data.get("url"):
        host = str(data["url"]).split("/")[2] if "://" in str(data["url"]) else str(data["url"])
        return "Открываю " + host[:60]
    return None


def _run_streaming(command: list[str], workdir: str, env: dict[str, str],
                   on_item: Callable[[dict[str, Any]], None],
                   on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Claude Code с потоком событий (stream-json): найденное по ходу (вызовы report_offer,
    запасной путь — строки ITEM в тексте) — в on_item сразу; что ИИ делает — в on_step;
    итог — событие result (в нём тот же structured_output, что и без потока)."""
    process = subprocess.Popen(command, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env)
    killed = threading.Event()

    def kill() -> None:
        killed.set()
        process.kill()

    timer = threading.Timer(CLAUDE_TIMEOUT, kill)
    timer.start()
    result: dict[str, Any] = {}
    try:
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    found, step = [], None
                    if block.get("type") == "text":
                        found = _stream_items(block.get("text") or "")
                    elif block.get("type") == "tool_use" and block.get("name") == REPORT_TOOL:
                        found = [block.get("input") or {}]
                    elif block.get("type") == "tool_use":
                        step = _step(block)
                    try:  # показ по ходу не должен ронять сам поиск
                        for item in found:
                            on_item(item)
                        if step and on_step:
                            on_step(step)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("streamed event: %s", exc)
            elif event.get("type") == "result":
                result = event
        process.wait()
    finally:
        timer.cancel()
    if killed.is_set():
        raise ResearchError("Claude не уложился в 15 минут")
    if not result:
        raise ResearchError(f"Claude Code завершился без ответа: {process.stderr.read()[-300:]}")
    return result


def _claude_code(prompt: str, model: str, schema: dict | None = None,
                 web: bool = True, on_item: Callable[[dict[str, Any]], None] | None = None,
                 on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
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
        # Пароль базы и ключ шифрования нужны службе, а не Claude Code.
        env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
        if on_item is not None:
            stream = list(command)
            stream[stream.index("--output-format") + 1] = "stream-json"
            # Инструмент «нашёл предложение» (report_mcp.py): только он, без других MCP.
            stream[stream.index("--allowedTools") + 1] += "," + REPORT_TOOL
            server = {"mcpServers": {"zakup": {"command": sys.executable, "args": ["-I", REPORT_SERVER]}}}
            output = _run_streaming(stream + ["--verbose", "--mcp-config", json.dumps(server)],
                                    workdir, env, on_item, on_step)
        else:
            try:
                done = subprocess.run(command, cwd=workdir, capture_output=True, text=True,
                                      timeout=CLAUDE_TIMEOUT, env=env)
            except subprocess.TimeoutExpired:
                raise ResearchError("Claude не уложился в 15 минут") from None
            try:
                output = json.loads(done.stdout)
            except json.JSONDecodeError:
                raise ResearchError(
                    f"Claude Code вернул не JSON: {done.stderr[-300:] or done.stdout[-300:]}") from None
    if output.get("is_error") or not output.get("structured_output"):
        raise ResearchError(f"Claude Code: {str(output.get('result') or output.get('subtype'))[:300]}")
    # Во сколько обошёлся бы запуск по API: считаем себестоимость запроса для тарифов.
    _usage.data.update(cost_usd=output.get("total_cost_usd"), usage=output.get("usage"),
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
