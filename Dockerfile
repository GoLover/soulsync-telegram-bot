FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/bot.sqlite3

RUN groupadd --system bot && useradd --system --gid bot bot \
    && install -d -o bot -g bot -m 0700 /data

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

USER bot
ENTRYPOINT ["soulsync-tg-bot"]
