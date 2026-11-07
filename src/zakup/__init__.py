import os

# Адрес сайта для ссылок в уведомлениях и ответах бота.
SITE_URL = os.environ.get("ZAKUP_SITE_URL", "https://zakup.sarsembai.com").rstrip("/")
