"""Access control middleware — only whitelisted users can interact with the bot."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.members_config import BOARD_MEMBERS

logger = logging.getLogger(__name__)


async def _log_activity(telegram_id: int, action_type: str):
    """Log a user interaction for engagement tracking."""
    try:
        from app.database import async_session, UserActivity
        async with async_session() as session:
            session.add(UserActivity(
                telegram_id=telegram_id,
                action_type=action_type,
                created_at=datetime.utcnow(),
            ))
            await session.commit()
    except Exception:
        pass  # never break the bot over analytics

# Build whitelist from members_config + env vars (all lowercase)
_ALLOWED: set[str] = set()

for _m in BOARD_MEMBERS:
    if _m.get("username"):
        _ALLOWED.add(_m["username"].lower())

for _u in os.getenv("CHAIRMAN_USERNAMES", "").split(","):
    if _u.strip():
        _ALLOWED.add(_u.strip().lower())

for _u in os.getenv("STAKEHOLDER_USERNAMES", "").split(","):
    if _u.strip():
        _ALLOWED.add(_u.strip().lower())


def is_allowed(username: str | None) -> bool:
    if not username:
        return False
    return username.lower() in _ALLOWED


class AccessMiddleware(BaseMiddleware):
    """Block all updates from users not in the whitelist. Silent drop — no response."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None

        if isinstance(event, Update):
            if event.message:
                user = event.message.from_user
            elif event.callback_query:
                user = event.callback_query.from_user
            elif event.inline_query:
                user = event.inline_query.from_user
            elif event.edited_message:
                user = event.edited_message.from_user
            elif event.channel_post:
                # Channel posts have no real user — always block
                return
            elif event.voice:
                user = getattr(event.voice, "from_user", None)

        if user is None:
            # Can't identify sender — block
            return

        if not is_allowed(user.username):
            logger.warning(
                "Blocked unauthorized access: user_id=%s username=%s",
                user.id,
                user.username,
            )
            return  # Silent drop — no response to unauthorized user

        # Track user activity
        if isinstance(event, Update):
            if event.message and event.message.voice:
                action = "voice"
            elif event.message:
                action = "message"
            elif event.callback_query:
                action = "callback"
            else:
                action = "other"
            await _log_activity(user.id, action)

        return await handler(event, data)
