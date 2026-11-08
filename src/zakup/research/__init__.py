"""Поиск товаров под лот: ИИ читает техспецификацию и ищет предложения в интернете.

Задачи ставит сайт (кнопка «Найти товары» на странице лота) в таблицу
lot_research, выполняет `zakup research`:

- provider "claude_code" — Claude Code на сервере по подписке владельца
  (`claude -p` с веб-поиском). Запускается на хосте, не в контейнере: там
  установлен и авторизован CLI (deploy/zakup-research.service);
- "anthropic" и "openai" — API с ключом администратора из настроек, в контейнере.

Ответ всегда в одной схеме (prompts.RESULT_SCHEMA): товары с ценой за единицу,
продавцом, городом, ссылкой и фото. Маржу считает сайт, а не модель.

Модули: prompts — подсказки и схемы; providers — вызовы моделей; context — данные
лота; results — обработка ответа; kaspi_query — кандидаты с Kaspi; queue — очередь
и исполнитель; summaries — выжимки техспецификаций.
"""

from zakup.research import providers
from zakup.research.context import clean_region, default_region, lot_context, manual_links
from zakup.research.kaspi_query import _kaspi_candidates
from zakup.research.prompts import RESULT_SCHEMA, build_prompt
from zakup.research.providers import (
    DEFAULT_MODELS,
    MODELS,
    PROVIDERS,
    SUMMARY_MODELS,
    ResearchError,
    api_key,
    light_model,
    provider_models,
)
from zakup.research.queue import (
    TARGETS,
    USER_DAILY_LIMIT,
    QuotaExceeded,
    enqueue,
    latest,
    latest_done,
    merged_done,
    run_job,
    work,
)
from zakup.research.results import clean, merge, without_loss
from zakup.research.summaries import request_summary, summary_of

__all__ = [
    "DEFAULT_MODELS", "MODELS", "PROVIDERS", "RESULT_SCHEMA", "SUMMARY_MODELS", "TARGETS", "USER_DAILY_LIMIT",
    "QuotaExceeded", "ResearchError", "_kaspi_candidates", "api_key", "build_prompt", "clean", "clean_region",
    "default_region", "enqueue", "latest", "latest_done", "light_model", "lot_context", "manual_links", "merge",
    "merged_done", "provider_models", "providers", "request_summary", "run_job", "summary_of", "without_loss",
    "work",
]
