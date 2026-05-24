# 🎬 Series Bot

Семейный Telegram-бот для учёта сериалов с постером, описанием, **встроенным русским трейлером** (играет прямо в чате) и pair-механикой "лайкнули оба → смотрим вместе".

Стек: Python 3.11 · aiogram 3 · SQLite · TMDB API · yt-dlp.

---

## Что бот умеет (MVP)

- `/add <название>` — поиск в TMDB, выбор из вариантов, карточка с постером
- `/list` — очередь "хочу посмотреть"
- `/watching` — "сейчас смотрю"
- `/watched` — досмотренное
- `/random` — случайный из очереди
- `/match` — сериалы, которые лайкнули **оба** из пары
- `/pair` — создать инвайт-код / вступить в пару
- Кнопки под карточкой: статусы 👀 ▶️ ✅ ❌ + оценки 👍 👎 + **🎥 трейлер**
- При нажатии на трейлер: бот ищет русский (TMDB → YouTube-поиск "русский трейлер"), скачивает ≤48MB, шлёт через `send_video` (играет в Telegram), кэширует `file_id` для мгновенной повторной отправки

---

## Локальный запуск

```bash
cd series-bot
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# отредактируй .env: BOT_TOKEN и TMDB_API_KEY

python -m bot.main
```

**Где взять ключи:**
1. `BOT_TOKEN`: пиши `@BotFather` в TG → `/newbot` → следуй инструкции
2. `TMDB_API_KEY`: https://developer.themoviedb.org → Settings → API → запросить ключ (бесплатно, выдают сразу)

Локально yt-dlp работает, если установлен `ffmpeg` в системе (для Windows: https://www.gyan.dev/ffmpeg/builds/, добавить в PATH).

---

## Деплой на Railway

1. Создай аккаунт на https://railway.app
2. New Project → Deploy from GitHub repo (или Deploy from Local — через `railway up` после `railway login`)
3. В Variables добавь:
   - `BOT_TOKEN`
   - `TMDB_API_KEY`
   - `DB_PATH=/data/bot.sqlite` (Railway сам подключит volume)
   - `TRAILER_TMP_DIR=/data/trailers`
4. Settings → Volumes → New Volume → Mount Path: `/data`
5. Deploy. Логи покажут `Starting bot.`

Dockerfile уже включает `ffmpeg` — yt-dlp в облаке работает из коробки.

**Альтернатива (Fly.io):**

```bash
fly launch --no-deploy
fly volumes create data --region fra --size 1
fly secrets set BOT_TOKEN=... TMDB_API_KEY=...
fly deploy
```

В `fly.toml` добавь mount: `[[mounts]] source="data" destination="/data"`.

---

## Структура

```
series-bot/
├── bot/
│   ├── main.py           # entry point
│   ├── config.py         # env
│   ├── db/
│   │   ├── models.py     # SQLAlchemy модели
│   │   └── repository.py # операции с БД
│   ├── services/
│   │   ├── tmdb.py       # клиент TMDB
│   │   └── trailer.py    # yt-dlp + поиск русского трейлера
│   ├── handlers/
│   │   ├── start.py      # /start /help /pair
│   │   └── series.py     # /add /list /match + кнопки
│   └── keyboards/
│       └── series_kb.py
├── data/                  # SQLite + временные mp4 (в .gitignore)
├── requirements.txt
├── Dockerfile
├── railway.json
├── .env.example
└── README.md
```

---

## Что дальше (следующие спринты)

- **Спринт 3:** Groq для умных рекомендаций `/suggest`, тег настроения `/mood`
- **Спринт 4:** парсинг TG-каналов `@kinotreilery` как ещё один источник трейлеров (через Telethon или `t.me/s/<channel>` HTML)
- **Спринт 5:** notifier новых сезонов (APScheduler), swipe-режим, импорт из Letterboxd

Подробности — в `../series-bot-plan.md`.

---

## Заметки

- **Кэш трейлера:** после первой отправки видео Telegram возвращает `file_id`. Сохраняем в БД (`series.trailer_file_id`), и второй раз бот отправляет видео **мгновенно** (не качает заново).
- **Лимит размера видео в Bot API — 50MB.** yt-dlp скачивает с фильтром `filesize<48M`, предпочитая 480p.
- **Если русского трейлера нет** — fallback на тот, что вернул TMDB (обычно английский).
- **БД:** SQLite более чем достаточно для семьи. Когда дойдёте до 10К+ сериалов или подключите друзей — мигрируем на PostgreSQL без боли (SQLAlchemy одинаково работает с обеими).
