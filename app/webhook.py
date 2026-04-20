"""Webhook sender — pushes events to Aishot."""

import os
import logging
import aiohttp

log = logging.getLogger(__name__)

AISHOT_WEBHOOK_URL = os.getenv("AISHOT_WEBHOOK_URL", "")
AISHOT_WEBHOOK_SECRET = os.getenv("AISHOT_WEBHOOK_SECRET", "")


async def push_event(event_type: str, data: dict):
    """Fire-and-forget: push event to Aishot. Never raises."""
    if not AISHOT_WEBHOOK_URL:
        return
    try:
        payload = {"event_type": event_type, "data": data}
        headers = {"Content-Type": "application/json"}
        if AISHOT_WEBHOOK_SECRET:
            headers["X-Webhook-Secret"] = AISHOT_WEBHOOK_SECRET

        async with aiohttp.ClientSession() as session:
            async with session.post(
                AISHOT_WEBHOOK_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Aishot webhook {event_type}: {resp.status}")
                else:
                    log.info(f"Aishot webhook sent: {event_type}")
    except Exception as e:
        log.warning(f"Aishot webhook error: {e}")
