"""Trakt.tv API клиент (СКЕЛЕТ — для будущей полноценной интеграции).

Trakt.tv даёт бесплатный API для синка прогресса серий между устройствами
и сервисами. Идея для бота: каждый партнёр привязывает свой Trakt-аккаунт
(через device-code OAuth), бот видит «оба досмотрели N-ю серию → пора
обсудить» и автоматически апдейтит UserSeries.

ЧТО НУЖНО ДЛЯ ПОЛНОЦЕННОЙ ИНТЕГРАЦИИ
====================================

1. Создать приложение на https://trakt.tv/oauth/applications:
   - Тип: application (НЕ Plex)
   - Redirect URI: urn:ietf:wg:oauth:2.0:oob  (device code flow без callback)
   - Получить TRAKT_CLIENT_ID и TRAKT_CLIENT_SECRET.

2. Добавить в Render env:
   TRAKT_CLIENT_ID=...
   TRAKT_CLIENT_SECRET=...

3. Новая команда /trakt_link в боте — запускает device-code flow:
   - POST /oauth/device/code → user_code + verification_url
   - бот шлёт «Открой trakt.tv/activate, введи код XXXX» с кнопкой URL
   - polling POST /oauth/device/token каждые 5с до получения access_token
   - сохранить (refresh_token, access_token, expires_at) в users.trakt_*

4. Новая колонка в users:
   trakt_access_token, trakt_refresh_token, trakt_expires_at

5. APScheduler job каждые ~30 мин:
   - для каждого юзера с trakt_access_token
   - GET /sync/watched/shows → список досмотренного на Trakt
   - сравнить с UserSeries — если на Trakt есть фильм/сериал которого
     нет в боте → опционально добавить или пометить как watched
   - GET /sync/history → последние действия для уведомления партнёра

6. При смене статуса в боте (watched) — POST /sync/history с этим сериалом
   (опционально, чтобы Trakt тоже знал).

ССЫЛКИ
======
- API docs: https://trakt.docs.apiary.io
- Device auth: https://trakt.docs.apiary.io/#reference/authentication-devices

Сейчас (заглушка): класс готов, методы возвращают NotImplemented.
Подключение в bot/main.py закомментировано до готовности.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TRAKT_API_BASE = "https://api.trakt.tv"


@dataclass
class TraktTokens:
    access_token: str
    refresh_token: str
    expires_at: int  # unix timestamp


@dataclass
class TraktDeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    interval: int  # poll seconds


class TraktClient:
    """Заглушка для будущей интеграции. Не используется в проде.

    Раскомментируй вызовы в main.py и реализуй методы по docstring выше
    когда будет готов к интеграции.
    """

    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 15.0) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = httpx.AsyncClient(
            timeout=timeout,
            base_url=TRAKT_API_BASE,
            headers={
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id,
            },
        )

    @classmethod
    def from_env(cls) -> Optional["TraktClient"]:
        cid = os.getenv("TRAKT_CLIENT_ID", "").strip()
        csec = os.getenv("TRAKT_CLIENT_SECRET", "").strip()
        if not cid or not csec:
            return None
        return cls(cid, csec)

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- OAuth device flow ----------

    async def request_device_code(self) -> TraktDeviceCode:
        """POST /oauth/device/code → код для пользователя."""
        raise NotImplementedError("TODO: device-code flow — см. docstring модуля")

    async def poll_device_token(self, device_code: str) -> Optional[TraktTokens]:
        """POST /oauth/device/token до получения токенов."""
        raise NotImplementedError("TODO")

    async def refresh_token(self, refresh_token: str) -> TraktTokens:
        """POST /oauth/token grant_type=refresh_token."""
        raise NotImplementedError("TODO")

    # ---------- Sync ----------

    async def get_watched_shows(self, access_token: str) -> list[dict]:
        """GET /sync/watched/shows — что юзер уже досмотрел."""
        raise NotImplementedError("TODO")

    async def get_history(self, access_token: str, *, limit: int = 50) -> list[dict]:
        """GET /sync/history — последние действия (для уведомлений партнёру)."""
        raise NotImplementedError("TODO")

    async def add_to_history(self, access_token: str, *, imdb_id: str) -> None:
        """POST /sync/history — отметить фильм/сериал как просмотренный на Trakt."""
        raise NotImplementedError("TODO")
