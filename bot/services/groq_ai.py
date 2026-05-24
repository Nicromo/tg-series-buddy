"""Groq AI клиент (OpenAI-совместимый API) — рекомендации, mood-поиск, vision."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

GROQ_BASE = "https://api.groq.com/openai/v1"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

logger = logging.getLogger(__name__)


@dataclass
class SuggestedSeries:
    title: str
    year: Optional[int]
    why: str


class GroqClient:
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile", *, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            base_url=GROQ_BASE,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        self._model = model

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


    async def transcribe_voice(self, audio_bytes: bytes, *, filename: str = "voice.oga") -> Optional[str]:
        """Speech-to-text через Groq Whisper. Возвращает распознанный текст."""
        try:
            files = {"file": (filename, audio_bytes, "audio/ogg")}
            data = {"model": "whisper-large-v3-turbo", "response_format": "json", "language": "ru"}
            # httpx с multipart
            resp = await self._client.post("/audio/transcriptions", data=data, files=files)
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip() or None
        except Exception as e:
            logger.warning("Whisper failed: %s", e)
            return None

    async def vision_recognize_series(self, image_bytes: bytes) -> Optional[str]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "На картинке — постер, скриншот или афиша сериала/фильма. "
                                "Извлеки ТОЛЬКО название (без года, без описания). "
                                "Если несколько — выбери основное. "
                                "Если это явно НЕ сериал/фильм — ответь NONE. "
                                "Верни ТОЛЬКО само название без кавычек и пояснений."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 60,
        }
        try:
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning("Vision recognize failed: %s", e)
            return None
        if not text or text.upper().startswith("NONE") or len(text) > 100:
            return None
        text = text.strip('"\'').strip()
        # Strip Russian quote chars manually (avoid escape headaches)
        for ch in ["«", "»", chr(0x201C), chr(0x201D)]:
            text = text.replace(ch, "")
        return text.strip() or None


    async def fix_query(self, query: str) -> Optional[str]:
        """Нормализует поисковый запрос — исправляет опечатки, дополняет.
        Возвращает исправленное название или None если запрос и так корректный."""
        system = (
            "Ты - помощник поиска фильмов/сериалов. На вход - запрос пользователя "
            "(может быть с опечатками или неполный). Если запрос явно содержит "
            "опечатку или неполное название известного фильма/сериала - "
            "верни ТОЛЬКО ИСПРАВЛЕННОЕ название без кавычек, без пояснений. "
            "Если запрос корректный или сложно определить - верни ровно слово SAME."
        )
        try:
            raw = (await self.chat(system, query)).strip()
        except Exception as e:
            logger.warning("Groq fix_query failed: %s", e)
            return None
        if not raw or raw.upper().startswith("SAME") or len(raw) > 80:
            return None
        for ch in ["\"", "'", "\u00ab", "\u00bb"]:
            raw = raw.replace(ch, "")
        return raw.strip() or None

    async def mood_search(self, mood: str, library_titles: list[str]) -> list[str]:
        """Из библиотеки юзера выбирает до 5 сериалов под запрос настроения."""
        if not library_titles:
            return []
        system = (
            "Ты — рекомендатель сериалов. Тебе дают список сериалов из библиотеки "
            "пользователя и запрос настроения. Верни ТОЛЬКО валидный JSON формата "
            '{"items":["name1","name2"]} — до 5 названий из СПИСКА, что лучше подходят. '
            "Используй РОВНО те названия что в списке."
        )
        lib = ", ".join(library_titles[:50])
        user = f"Настроение: {mood}\n\nБиблиотека: {lib}\n\nВерни до 5 в JSON items."
        try:
            raw = await self.chat(system, user, json_mode=True)
            data = json.loads(raw)
            items = data.get("items", [])
            return [str(x).strip() for x in items if str(x).strip()][:5]
        except Exception as e:
            logger.warning("mood_search failed: %s", e)
            return []

    async def suggest_for_pair(
        self,
        *,
        likes_a: list[str],
        likes_b: list[str],
        dislikes_a: list[str],
        dislikes_b: list[str],
        already_in_queue: list[str],
        mood_hint: Optional[str] = None,
    ) -> list[SuggestedSeries]:
        system = (
            "Ты — рекомендатель сериалов для пары. Отдай ТОЛЬКО JSON "
            '{"items":[{"title":"...","year":2020,"why":"..."}]}. '
            "Title — русский или оригинал. Why — короткая фраза почему ИМ зайдёт."
        )
        bits = []
        if likes_a:
            bits.append(f"Партнёр A лайкнул: {', '.join(likes_a[:10])}")
        if dislikes_a:
            bits.append(f"Партнёру A не зашло: {', '.join(dislikes_a[:5])}")
        if likes_b:
            bits.append(f"Партнёр B лайкнул: {', '.join(likes_b[:10])}")
        if dislikes_b:
            bits.append(f"Партнёру B не зашло: {', '.join(dislikes_b[:5])}")
        if already_in_queue:
            bits.append(f"Уже в очереди (НЕ предлагать): {', '.join(already_in_queue[:20])}")
        if mood_hint:
            bits.append(f"Настроение: {mood_hint}")

        user = (
            ("\n".join(bits) if bits else "Истории мало — предложи популярные сериалы.")
            + "\n\nПредложи 3 сериала которые зайдут ОБОИМ. JSON items."
        )

        try:
            raw = await self.chat(system, user, json_mode=True)
            data = json.loads(raw)
            items = data.get("items", [])
            result: list[SuggestedSeries] = []
            for it in items[:3]:
                title = (it.get("title") or "").strip()
                if not title:
                    continue
                year = it.get("year")
                if isinstance(year, str) and year.isdigit():
                    year = int(year)
                elif not isinstance(year, int):
                    year = None
                result.append(SuggestedSeries(title=title, year=year, why=it.get("why") or ""))
            return result
        except Exception as e:
            logger.warning("Groq suggest failed: %s", e)
            return []
