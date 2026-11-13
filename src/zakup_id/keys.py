"""Первая настройка: python -m zakup_id.keys — печатает строки для deploy/kz/.env.

Закрытый ключ остаётся только в Казахстане; открытый (ID_PUBLIC_KEY) — основному сайту.
Вывод — в файл (python -m zakup_id.keys >> deploy/kz/.env), не на экран и не в чат."""

import secrets

from zakup_id import token

if __name__ == "__main__":
    private, public = token.generate_keys()
    print(f"ID_SIGNING_KEY={private}")
    print(f"ID_DEVICE_KEY={secrets.token_urlsafe(32)}")
    print(f"ID_SERVICE_KEY={secrets.token_urlsafe(32)}")
    print(f"ID_DB_PASSWORD={secrets.token_urlsafe(24)}")
    print(f"# основному сайту: ID_PUBLIC_KEY={public}")
