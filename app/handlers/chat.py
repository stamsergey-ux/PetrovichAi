"""AI chat handler — free-form conversation about meetings and tasks."""
from __future__ import annotations

import io
import json
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.database import async_session, Task, Member, Meeting, AgendaRequest, ScheduledMeeting
from app.ai_service import chat_with_context, generate_agenda
from app.rag import search_relevant_chunks
from app.gantt import generate_gantt_pdf
from app.utils import is_chairman

router = Router()



@router.callback_query(F.data == "adv_schedule")
async def cb_adv_schedule(callback: CallbackQuery):
    """Prompt user to schedule a meeting."""
    await callback.answer()
    await callback.message.answer(
        "📅 <b>Назначить совещание</b>\n\n"
        "Напиши в формате:\n"
        "<i>Назначь совещание ДД.ММ.ГГГГ Название</i>\n\n"
        "Пример:\n"
        "<i>Назначь совещание 15.03.2026 Итоги Q1</i>",
        parse_mode="HTML",
    )


async def _get_my_tasks_summary(telegram_id: int) -> str | None:
    """Get tasks assigned to the specific user."""
    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == telegram_id)
        )).scalar_one_or_none()
        if not member:
            return None
        result = await session.execute(
            select(Task)
            .where(Task.assignee_id == member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
            .order_by(Task.deadline.asc())
        )
        tasks = result.scalars().all()
    if not tasks:
        return "No open tasks assigned."
    lines = []
    for t in tasks:
        deadline = t.deadline.strftime("%d.%m.%Y") if t.deadline else "no deadline"
        lines.append(f"#{t.id} [{t.status}] {t.title}, deadline: {deadline}")
    return "\n".join(lines)


async def _get_task_context(task_id: int) -> str | None:
    """Fetch full task details for AI context."""
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            return None
        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None
        meeting = await session.get(Meeting, task.meeting_id) if task.meeting_id else None
    assignee_name = assignee.name if assignee else "не назначен"
    deadline = task.deadline.strftime("%d.%m.%Y") if task.deadline else "без срока"
    meeting_str = (meeting.title or f"Совещание {meeting.date.strftime('%d.%m.%Y')}") if meeting else "Поручения председателя"
    lines = [
        f"#{task.id} [{task.status}] {task.title}",
        f"Исполнитель: {assignee_name}, Дедлайн: {deadline}",
        f"Протокол: {meeting_str}",
    ]
    if task.description:
        lines.append(f"Описание: {task.description}")
    if task.context_quote:
        lines.append(f"Контекст из протокола: {task.context_quote}")
    return "\n".join(lines)


async def _ai_chat(message: Message, override_text: str | None = None, state: FSMContext | None = None):
    """Handle free-form AI chat with RAG context."""
    from app.utils import is_stakeholder
    user = message.from_user
    user_name = user.first_name or user.username or "Пользователь"
    user_text = override_text or message.text

    if is_chairman(user.username):
        user_role = "Председатель совета директоров"
    elif is_stakeholder(user.username):
        user_role = "Акционер"
    else:
        user_role = "Член совета директоров"

    # Check if user was looking at a specific task (context from task_detail)
    task_context = None
    if state:
        data = await state.get_data()
        last_task_id = data.get("last_task_id")
        if last_task_id:
            task_context = await _get_task_context(last_task_id)

    # Search relevant meeting chunks
    chunks = await search_relevant_chunks(user_text, limit=5)
    tasks_summary = await _get_tasks_summary()
    my_tasks = await _get_my_tasks_summary(user.id)

    await message.answer("🤖 Думаю...")

    response = await chat_with_context(
        user_message=user_text,
        user_name=user_name,
        context_chunks=chunks,
        tasks_summary=tasks_summary,
        user_role=user_role,
        my_tasks_summary=my_tasks,
        task_context=task_context,
    )

    # Split long responses
    if len(response) > 4000:
        parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
        for part in parts:
            await message.answer(part)
    else:
        await message.answer(response)
