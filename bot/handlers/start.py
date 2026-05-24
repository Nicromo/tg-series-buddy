"""Регистрация, /start, /help, /pair."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import repository as repo
from ..db.models import Pair

HELP_TEXT = (
    "🎬 <b>Семейный учёт сериалов</b>\n\n"
    "<b>Базовые команды:</b>\n"
    "/add &lt;название&gt; — найти и добавить сериал\n"
    "/list — что хотим посмотреть\n"
    "/watching — что смотрим сейчас\n"
    "/watched — что досмотрели\n"
    "/random — случайный из очереди\n"
    "/match — что лайкнули вы оба\n\n"
    "<b>Пара:</b>\n"
    "/pair — получить инвайт-код (для жены)\n"
    "/pair &lt;код&gt; — присоединиться к чужой паре"
)


def make_router(session_factory: async_sessionmaker) -> Router:
    router = Router(name="start")

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        async with session_factory() as session:
            await repo.get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
            await session.commit()
        await message.answer("Привет! 👋\n\n" + HELP_TEXT, parse_mode="HTML")

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode="HTML")

    @router.message(Command("pair"))
    async def cmd_pair(message: Message) -> None:
        # /pair          → создать/показать инвайт-код
        # /pair <code>   → присоединиться к паре
        parts = (message.text or "").split(maxsplit=1)
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session,
                tg_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )

            if len(parts) == 1:
                if user.pair_id:
                    pair = await session.get(Pair, user.pair_id)
                    code = pair.invite_code if pair else "(ошибка)"
                else:
                    pair = await repo.create_pair_for_user(session, user)
                    code = pair.invite_code
                await session.commit()
                await message.answer(
                    f"🔗 Ваш инвайт-код: <code>{code}</code>\n\n"
                    f"Перешлите его жене/партнёру. Они напишут:\n"
                    f"<code>/pair {code}</code>",
                    parse_mode="HTML",
                )
                return

            code = parts[1].strip()
            pair = await repo.join_pair_by_code(session, user, code)
            if pair is None:
                await message.answer("❌ Код не найден. Проверь правильность.")
                return
            await session.commit()
            await message.answer(
                "✅ Вы в одной паре. Теперь /match покажет ваши общие лайки."
            )

    return router
