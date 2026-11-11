"""Крошечный MCP-сервер для Claude Code: один инструмент report_offer — «нашёл предложение».

Ничего не делает и ничего не хранит: служба читает вызовы инструмента из потока событий
Claude Code (stream-json) и сразу показывает предложение на странице лота. Инструмент
нужен, чтобы модель отдавала находки по ходу поиска: просьбу «пиши строкой текста» она
игнорирует, а инструменты вызывает охотно.

Запускается Claude Code как отдельный процесс (`python -I report_mcp.py`), без зависимостей
и без доступа к базе: протокол MCP — JSON-RPC 2.0 построчно через stdin/stdout.
"""

import json
import sys

TOOL = {
    "name": "report_offer",
    "description": ("Сообщить человеку о подходящем предложении сразу, как только проверил его страницу. "
                    "Вызывай для каждого предложения по ходу поиска, не копи до конца."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "seller": {"type": "string"}, "city": {"type": "string"},
            "in_region": {"type": "boolean"}, "price": {"type": ["number", "null"]},
            "price_unit": {"type": "string"}, "unit_price": {"type": ["number", "null"]},
            "url": {"type": "string"}, "image": {"type": ["string", "null"]},
            "fit": {"type": "string", "enum": ["полностью", "частично", "аналог"]},
            "specs": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "value": {"type": "string"}}}},
            "checked": {"type": "boolean"},
        },
        "required": ["name", "url"],
    },
}


def answer(message: dict) -> dict | None:
    method, ident = message.get("method"), message.get("id")
    if ident is None:  # уведомление (notifications/initialized и т.п.) — без ответа
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion") or "2024-11-05"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "zakup-report", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "Показано человеку. Ищи дальше."}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "unknown method"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if (reply := answer(message)) is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
