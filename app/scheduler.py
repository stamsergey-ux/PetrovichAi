"""Scheduler: only marks overdue tasks in DB. No push notifications to users."""
from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select, and_

from app.database import async_session, Task


async def mark_overdue_tasks():
    """Mark tasks past deadline as overdue. No notifications sent."""
    now = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(and_(
                Task.status.in_(["new", "in_progress"]),
                Task.deadline != None,
                Task.deadline < now,
            ))
        )
        overdue_tasks = result.scalars().all()
        for task in overdue_tasks:
            task.status = "overdue"
        if overdue_tasks:
            await session.commit()


async def run_scheduler(bot: Bot):
    """Main scheduler loop — marks overdue tasks every hour. No notifications."""
    while True:
        try:
            await mark_overdue_tasks()
        except Exception as e:
            print(f"Scheduler error: {e}")
        await asyncio.sleep(3600)
