FROM python:3.11-slim

# ffmpeg нужен yt-dlp для сшивки аудио и видео (когда формат раздельный)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# На Railway смонтируется volume в /data — пути берутся из env
ENV DB_PATH=/data/bot.sqlite
ENV TRAILER_TMP_DIR=/data/trailers
RUN mkdir -p /data/trailers

CMD ["python", "-m", "bot.main"]
