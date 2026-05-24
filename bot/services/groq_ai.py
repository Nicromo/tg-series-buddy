"""Groq AI клиент (OpenAI-совместимый API) — рекомендации, mood-search, vision."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

GROQ_BASE = "https://api.groq.com/openai/v1"
# Vision-capable model (multimodal). For text-only — use Settings.groq_model.
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

logger = logging.getLogger(__name__)


@dataclass
class SuggestedSeries:
    title: str
    year: Optional[int]
    why: str


class GroqClient:
    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        *,
        timeout: float = 30.0,
    ) -> None:
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

    async def vision_recognize_series(self, image_bytes: bytes) -> Optional[str]:
        """По фотографии скрина/постера/афиши извлекает НАЗВАНИЕ сериала.

        Возвращает строку с названием или None, если не нашёл.
        """
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
                                "Если на картинке несколько названий — выбери основное. "
                                "Если это явно НЕ сериал/фильм — ответь словом NONE. "
                                "Верни ТОЛЬКО само название, без кавычек, без пояснений."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
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
        # remove quotes
        text = text.strip('"\'«»“”').strip()
        return text or None

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
            "Ты — рекомендатель сериалов для пары. Отдаёшь ТОЛЬКО валидный JSON "
            'формата {"items":[{"title":"...","year":2020,"why":"..."}]}. '
            "Title — на русском или оригинальном языке. "
            "Why — одна короткая фраза почему именно ИМ зайдёт."
        )
        bits = []
        if likes_a:
            bits.append(f"Партнёр A любит: {', '.join(likes_a[:10])}")
        if dislikes_a:
            bits.append(f"Партнёру A не зашло: {', '.join(dislikes_a[:5])}")
        if likes_b:
            bits.append(f"Партнёр B любит: {', '.join(likes_b[:10])}")
        if dislikes_b:
            bits.append(f"Партнёру B не зашло: {', '.join(dislikes_b[:5])}")
        if already_in_queue:
            bits.append(f"Уже в очереди (НЕ предлагать): {', '.join(already_in_queue[:20])}")
        if mood_hint:
            bits.append(f"Настроение: {mood_hint}")

        user = (
            ("\n".join(bits) if bits else "Истории мало — предложи популярные сериалы.")
            + "\n\nПредложи 3 сериала которые зайдут ОБОИМ. "
            "Верни JSON с полем items: массив объектов {title, year, why}."
        )

        raw = await self.chat(system, user, json_mode=True)
        try:
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
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Groq returned non-JSON: %s — raw=%s", e, raw[:200])
            return []
