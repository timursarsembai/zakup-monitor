FROM python:3.12-slim
WORKDIR /app
# Сначала библиотеки (меняются редко) — отдельным слоем: правка кода не переустанавливает их.
COPY pyproject.toml ./
RUN mkdir -p src/zakup && touch src/zakup/__init__.py \
    && pip install --no-cache-dir -e '.[dev]'
COPY src ./src
COPY tests ./tests
# Том фото товаров: новый том берёт владельца папки из образа — сайт (nobody) может писать.
RUN mkdir -p /data/media && chown nobody:nogroup /data/media
USER nobody
ENTRYPOINT ["zakup"]
