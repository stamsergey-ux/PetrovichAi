"""AI Secretary for Board of Directors — Telegram Bot."""

import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from app.database import init_db
from app.handlers import onboarding, protocol, tasks, voice, meetings, chat, stakeholder
from app.scheduler import run_scheduler


async def main():
    # Init database
    os.makedirs("data", exist_ok=True)
    await init_db()

    # Init bot
    bot = Bot(
        token=os.getenv("BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Register handlers (order matters — chat is catch-all, must be last)
    dp.include_router(onboarding.router)
    dp.include_router(protocol.router)
    dp.include_router(tasks.router)
    dp.include_router(stakeholder.router)  # FSM states must come before voice/chat
    dp.include_router(voice.router)
    dp.include_router(meetings.router)
    dp.include_router(chat.router)  # Must be last — catches all text messages

    # Start scheduler in background
    asyncio.create_task(run_scheduler(bot))

    # Clean up any stale webhook before polling
    await bot.delete_webhook(drop_pending_updates=True)

    print("Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
