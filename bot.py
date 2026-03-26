"""AI Secretary for Board of Directors — Telegram Bot."""

import asyncio
import logging
import os
import subprocess
import sys

# Ensure asyncpg is installed before any imports that need it
try:
    import asyncpg
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "asyncpg==0.30.0", "--quiet"])

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

from app.database import init_db, seed_members_from_config
from app.handlers import onboarding, protocol, tasks, voice, meetings, chat, stakeholder, task_verify, materials, chairman_tasks, personal
from app.middleware import AccessMiddleware
from app.scheduler import run_scheduler


async def main():
    # Init database
    os.makedirs("data", exist_ok=True)
    await init_db()
    await seed_members_from_config()

    # Start web panel alongside the bot
    try:
        import uvicorn
        from webapp.main import app as web_app
        port = int(os.getenv("PORT", "8080"))
        config = uvicorn.Config(web_app, host="0.0.0.0", port=port, log_level="warning")
        server = uvicorn.Server(config)
        asyncio.create_task(server.serve())
        print(f"Web panel started on port {port}")
    except Exception as e:
        print(f"Web panel not started: {e}")

    # Init bot
    bot = Bot(
        token=os.getenv("BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Security: block all updates from users not in the whitelist
    dp.update.outer_middleware(AccessMiddleware())

    # Register handlers (order matters — chat is catch-all, must be last)
    dp.include_router(onboarding.router)
    dp.include_router(protocol.router)
    dp.include_router(tasks.router)
    dp.include_router(stakeholder.router)  # FSM states must come before voice/chat
    dp.include_router(chairman_tasks.router)  # FSM states must come before voice/chat
    dp.include_router(task_verify.router)  # FSM states must come before chat
    dp.include_router(personal.router)  # Personal tasks FSM before voice/chat
    dp.include_router(materials.router)
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
