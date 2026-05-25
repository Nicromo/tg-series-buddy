FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# SQLite fallback на Railway/Render с volume в /data
ENV DB_PATH=/data/bot.sqlite
RUN mkdir -p /data

CMD ["python", "-m", "bot.main"]
