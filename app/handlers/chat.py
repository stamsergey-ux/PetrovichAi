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


async def _get_tasks_summary() -> str:
    """Get summary of all open tasks for AI context."""
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
            .order_by(Task.deadline.asc())
        )
        tasks = result.scalars().all()
    if not tasks:
        return "No open tasks."
    lines = []
    for t in tasks:
        deadline = t.deadline.strftime("%d.%m.%Y") if t.deadline else "no deadline"
        assignee_id = t.assignee_id or "unassigned"
        lines.append(f"#{t.id} [{t.status}] {t.title}, assignee_id={assignee_id}, deadline: {deadline}")
    return "\n".join(lines)


async def _show_last_protocol(message: Message):
    """Show last confirmed protocol — available to all members."""
    from html import escape
    async with async_session() as session:
        result = await session.execute(
            select(Meeting)
            .where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc())
            .limit(1)
        )
        meeting = result.scalar_one_or_none()

    if not meeting:
        await message.answer("📭 <b>Пока нет сохранённых протоколов.</b>", parse_mode="HTML")
        return

    date_str = meeting.date.strftime('%d.%m.%Y')
    title = escape(meeting.title or "Без названия")
    summary = escape(meeting.summary or "—")

    text = f"📝 <b>ПРОТОКОЛ</b>\n\n<b>{title}</b>\n📅 {date_str}\n\n{summary}\n"

    if meeting.decisions:
        try:
            decisions = json.loads(meeting.decisions)
            if decisions:
                text += f"\n⚖️ <b>РЕШЕНИЯ:</b>\n"
                for d in decisions:
                    text += f"  • {escape(d['text'])}\n"
        except (json.JSONDecodeError, KeyError):
            pass

    if meeting.open_questions:
        try:
            questions = json.loads(meeting.open_questions)
            if questions:
                text += f"\n❓ <b>ОТКРЫТЫЕ ВОПРОСЫ:</b>\n"
                for q in questions:
                    text += f"  • {escape(q['text'])}\n"
        except (json.JSONDecodeError, KeyError):
            pass

    if len(text) > 4000:
        text = text[:4000] + "\n\n... <i>протокол обрезан</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks")]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _send_agenda(message: Message):
    """Generate and send agenda — chairman only."""
    from html import escape
    async with async_session() as session:
        # Recent meetings for context
        meetings = (await session.execute(
            select(Meeting).where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc()).limit(3)
        )).scalars().all()
        meetings_context = "\n\n".join(
            f"{m.title} ({m.date.strftime('%d.%m.%Y')}): {(m.summary or '')[:300]}"
            for m in meetings
        ) or "Нет данных"

        # Open tasks
        open_tasks_result = await session.execute(
            select(Task).where(Task.status.in_(["new", "in_progress"]))
        )
        open_tasks = open_tasks_result.scalars().all()
        open_tasks_text = "\n".join(
            f"#{t.id} {t.title} (deadline: {t.deadline.strftime('%d.%m.%Y') if t.deadline else '—'})"
            for t in open_tasks
        ) or "Нет"

        # Overdue
        overdue_result = await session.execute(
            select(Task).where(Task.status == "overdue")
        )
        overdue_tasks = overdue_result.scalars().all()
        overdue_text = "\n".join(
            f"#{t.id} {t.title} (deadline: {t.deadline.strftime('%d.%m.%Y') if t.deadline else '—'})"
            for t in overdue_tasks
        ) or "Нет"

        # Agenda items from meetings
        agenda_items = ""
        for m in meetings:
            if m.agenda_items_next:
                try:
                    items = json.loads(m.agenda_items_next)
                    if items:
                        agenda_items += f"\nИз совещания {m.date.strftime('%d.%m.%Y')}:\n"
                        for a in items:
                            agenda_items += f"  • {a.get('topic', '?')} ({a.get('presenter', '?')})\n"
                except (json.JSONDecodeError, KeyError):
                    pass

        # Pending agenda requests from members
        pending_requests = (await session.execute(
            select(AgendaRequest, Member)
            .join(Member, AgendaRequest.member_id == Member.id)
            .where(AgendaRequest.is_approved != False)
            .where(AgendaRequest.is_included == False)
        )).all()
        if pending_requests:
            agenda_items += "\n\nЗапросы от членов СД:\n"
            for req, member in pending_requests:
                name = member.display_name or member.first_name or member.username or "?"
                dur = f", {req.duration_minutes} мин" if req.duration_minutes else ""
                agenda_items += f"  • {req.topic} (от {name}{dur})\n"

    await message.answer("🤖 Генерирую повестку...")

    agenda = await generate_agenda(
        meetings_context=meetings_context,
        open_tasks=open_tasks_text,
        overdue_tasks=overdue_text,
        agenda_items_from_meetings=agenda_items or "Нет",
    )

    if len(agenda) > 4000:
        parts = [agenda[i:i+4000] for i in range(0, len(agenda), 4000)]
        for part in parts:
            await message.answer(part)
    else:
        await message.answer(agenda)


@router.message(F.text.regexp(r"(?i)добавь в адженду[:\s]+(.+)"))
async def handle_agenda_add(message: Message):
    """Handle free-text agenda item submission from any member."""
    import re
    match = re.search(r"(?i)добавь в адженду[:\s]+(.+)", message.text)
    if not match:
        return
    raw = match.group(1).strip()

    # Parse "тема, 15 мин" format
    duration = None
    topic = raw
    dur_match = re.search(r",?\s*(\d+)\s*мин", raw)
    if dur_match:
        duration = int(dur_match.group(1))
        topic = raw[:dur_match.start()].strip().rstrip(",").strip()

    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == message.from_user.id)
        )).scalar_one_or_none()
        if not member:
            await message.answer("⚠️ Ты не зарегистрирован. Нажми /start")
            return

        req = AgendaRequest(
            member_id=member.id,
            topic=topic,
            duration_minutes=duration,
        )
        session.add(req)
        await session.commit()

    dur_text = f" ({duration} мин)" if duration else ""
    await message.answer(
        f"✅ <b>Добавлено в адженду</b>\n\n"
        f"📌 {topic}{dur_text}\n\n"
        f"<i>Председатель увидит это при генерации повестки.</i>",
        parse_mode="HTML",
    )


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
