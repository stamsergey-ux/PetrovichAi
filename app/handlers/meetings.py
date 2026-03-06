"""Meeting cycle handlers: schedule, agenda requests, status collection, analytics."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from html import escape

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func

from app.database import (
    async_session, Task, Member, Meeting, ScheduledMeeting,
    AgendaRequest, StatusReport, StrategicGoal,
)
from app.ai_service import generate_agenda
from app.utils import is_chairman

logger = logging.getLogger(__name__)
router = Router()


# ─── Schedule a meeting ───────────────────────────────────────────────

@router.message(F.text.lower().startswith("назначь совещание"))
async def schedule_meeting(message: Message):
    """Schedule a new meeting. Format: 'назначь совещание DD.MM.YYYY [название]'"""
    if not is_chairman(message.from_user.username):
        await message.answer("⛔ Назначение совещаний доступно администраторам.")
        return

    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "📅 Формат: <code>назначь совещание 12.03.2026 Совет директоров</code>",
            parse_mode="HTML",
        )
        return

    date_str = parts[1] if len(parts) > 1 else ""
    title = parts[2] if len(parts) > 2 else "Совещание СД"

    # Try to parse various date formats
    meeting_date = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            # Handle "назначь совещание 12.03.2026" where parts[1] is "совещание"
            meeting_date = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue

    if not meeting_date:
        # Try parsing from the rest of the text
        rest = message.text.lower().replace("назначь совещание", "").strip()
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                date_part = rest.split()[0]
                meeting_date = datetime.strptime(date_part, fmt)
                title = " ".join(rest.split()[1:]) or "Совещание СД"
                break
            except (ValueError, IndexError):
                continue

    if not meeting_date:
        await message.answer("⚠️ Не смог распознать дату. Формат: ДД.ММ.ГГГГ")
        return

    async with async_session() as session:
        scheduled = ScheduledMeeting(
            scheduled_date=meeting_date,
            title=title,
        )
        session.add(scheduled)
        await session.commit()

    date_display = meeting_date.strftime("%d.%m.%Y")
    await message.answer(
        f"✅ Совещание назначено!\n\n"
        f"📅 <b>{date_display}</b>\n"
        f"📌 {escape(title)}\n\n"
        f"За 24ч до совещания я:\n"
        f"  • Соберу статусы задач у участников\n"
        f"  • Сгенерирую адженду\n"
        f"  • Разошлю повестку всем",
        parse_mode="HTML",
    )


# ─── Agenda requests from members ────────────────────────────────────

@router.message(F.text.lower().startswith("добавь в адженду"))
async def add_agenda_item(message: Message):
    """Any member can request an agenda item. Format: 'добавь в адженду: тема'"""
    text = message.text
    # Extract topic after "добавь в адженду" (with or without colon)
    topic = text.split(":", 1)[1].strip() if ":" in text else text[len("добавь в адженду"):].strip()

    if not topic or len(topic) < 3:
        await message.answer("📌 Укажи тему: <code>добавь в адженду: обсудить бюджет Q2</code>", parse_mode="HTML")
        return

    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == message.from_user.id)
        )).scalar_one_or_none()

        if not member:
            await message.answer("Нажми /start для регистрации.")
            return

        # Find next scheduled meeting
        next_meeting = (await session.execute(
            select(ScheduledMeeting)
            .where(ScheduledMeeting.is_completed == False)
            .order_by(ScheduledMeeting.scheduled_date.asc())
            .limit(1)
        )).scalar_one_or_none()

        request = AgendaRequest(
            member_id=member.id,
            topic=topic,
            scheduled_meeting_id=next_meeting.id if next_meeting else None,
        )
        session.add(request)
        await session.commit()

    name = member.display_name or member.first_name or "Участник"
    meeting_info = f" (совещание {next_meeting.scheduled_date.strftime('%d.%m.%Y')})" if next_meeting else ""
    await message.answer(
        f"📌 Пункт добавлен в адженду{escape(meeting_info)}!\n\n"
        f"👤 {escape(name)}\n"
        f"📋 {escape(topic)}",
        parse_mode="HTML",
    )


# ─── Pre-meeting status collection ───────────────────────────────────

async def send_status_requests(bot: Bot, scheduled_meeting_id: int):
    """Send status requests to all members with open tasks before a meeting."""
    async with async_session() as session:
        scheduled = await session.get(ScheduledMeeting, scheduled_meeting_id)
        if not scheduled or scheduled.status_collection_sent:
            return

        # Get all members with open tasks
        result = await session.execute(
            select(Task, Member)
            .join(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue"]))
            .order_by(Member.id)
        )
        rows = result.all()

        # Group by member
        by_member: dict[int, dict] = {}
        for task, member in rows:
            if member.telegram_id not in by_member:
                by_member[member.telegram_id] = {"member": member, "tasks": []}
            by_member[member.telegram_id]["tasks"].append(task)

        meeting_date = scheduled.scheduled_date.strftime("%d.%m.%Y")

        for tg_id, data in by_member.items():
            name = data["member"].display_name or data["member"].first_name or ""
            text = f"📋 <b>Запрос статуса перед совещанием</b>\n"
            text += f"📅 {meeting_date}\n\n"
            text += f"Привет, {escape(name)}! У тебя {len(data['tasks'])} открытых задач:\n\n"

            for t in data["tasks"]:
                status_icon = {"new": "⬜", "in_progress": "🔵", "overdue": "🔴"}.get(t.status, "⬜")
                deadline = t.deadline.strftime("%d.%m") if t.deadline else "—"
                text += f"  {status_icon} #{t.id} {escape(t.title[:60])}\n"
                text += f"      📅 {deadline}\n\n"

            text += "Ответь голосовым или текстом — расскажи статус по задачам.\n"
            text += "Я передам сводку председателю."

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Все задачи в работе",
                    callback_data=f"status_all_ok:{scheduled_meeting_id}",
                )]
            ])

            try:
                await bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=keyboard)
            except Exception as e:
                logger.warning(f"Failed to send status request to {tg_id}: {e}")

        scheduled.status_collection_sent = True
        await session.commit()

    logger.info(f"Status requests sent to {len(by_member)} members for meeting {scheduled_meeting_id}")


@router.callback_query(F.data.startswith("status_all_ok:"))
async def cb_status_all_ok(callback: CallbackQuery):
    """Quick status: all tasks are in progress."""
    meeting_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == callback.from_user.id)
        )).scalar_one_or_none()

        if member:
            report = StatusReport(
                member_id=member.id,
                scheduled_meeting_id=meeting_id,
                status_text="Все задачи в работе, без блокеров.",
            )
            session.add(report)
            await session.commit()

    await callback.message.answer("✅ Статус принят: все задачи в работе.")
    await callback.answer("Принято!")


# ─── Auto agenda distribution ────────────────────────────────────────

async def generate_and_send_agenda(bot: Bot, scheduled_meeting_id: int):
    """Generate agenda and send to all members 24h before meeting."""
    async with async_session() as session:
        scheduled = await session.get(ScheduledMeeting, scheduled_meeting_id)
        if not scheduled or scheduled.agenda_sent:
            return

        # Collect data for agenda generation
        meetings = (await session.execute(
            select(Meeting).where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc()).limit(5)
        )).scalars().all()

        open_tasks = (await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress"]))
            .order_by(Task.deadline.asc())
        )).all()

        overdue_tasks = (await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status == "overdue")
        )).all()

        # Agenda requests from members
        agenda_requests = (await session.execute(
            select(AgendaRequest, Member)
            .join(Member, AgendaRequest.member_id == Member.id)
            .where(AgendaRequest.scheduled_meeting_id == scheduled.id)
            .where(AgendaRequest.is_included == False)
        )).all()

        # Status reports
        status_reports = (await session.execute(
            select(StatusReport, Member)
            .join(Member, StatusReport.member_id == Member.id)
            .where(StatusReport.scheduled_meeting_id == scheduled.id)
        )).all()

    # Build context strings
    meetings_ctx = ""
    prev_agenda_items = ""
    for m in meetings:
        meetings_ctx += f"\n[{m.date.strftime('%d.%m.%Y')}] {m.title}\n{m.summary[:500]}\n"
        if m.agenda_items_next:
            try:
                items = json.loads(m.agenda_items_next)
                for item in items:
                    prev_agenda_items += f"- {item.get('topic', '?')} (presenter: {item.get('presenter', '?')})\n"
            except json.JSONDecodeError:
                pass

    open_text = "\n".join(
        f"#{t.id} {t.title} -> {m.name if m else '?'}, deadline: {t.deadline}" for t, m in open_tasks
    ) or "None"

    overdue_text = "\n".join(
        f"#{t.id} {t.title} -> {m.name if m else '?'}, deadline: {t.deadline}" for t, m in overdue_tasks
    ) or "None"

    # Add member requests to agenda context
    if agenda_requests:
        prev_agenda_items += "\nЗАПРОСЫ ОТ УЧАСТНИКОВ:\n"
        for req, member in agenda_requests:
            name = member.display_name or member.first_name or "?"
            prev_agenda_items += f"- {name}: {req.topic}\n"

    # Add status reports context
    if status_reports:
        prev_agenda_items += "\nСТАТУСЫ ОТ УЧАСТНИКОВ:\n"
        for report, member in status_reports:
            name = member.display_name or member.first_name or "?"
            prev_agenda_items += f"- {name}: {report.status_text[:200]}\n"

    agenda_text = await generate_agenda(meetings_ctx, open_text, overdue_text, prev_agenda_items or "None")

    # Save agenda
    async with async_session() as session:
        scheduled = await session.get(ScheduledMeeting, scheduled_meeting_id)
        scheduled.agenda_text = agenda_text
        scheduled.agenda_sent = True

        # Mark agenda requests as included
        for req, _ in agenda_requests:
            req_obj = await session.get(AgendaRequest, req.id)
            req_obj.is_included = True

        await session.commit()

    # Send to all members
    meeting_date = scheduled.scheduled_date.strftime("%d.%m.%Y")
    full_text = f"📌 <b>ПОВЕСТКА СОВЕЩАНИЯ</b>\n"
    full_text += f"📅 {meeting_date} — {escape(scheduled.title or '')}\n\n"
    full_text += escape(agenda_text)

    async with async_session() as session:
        all_members = (await session.execute(
            select(Member).where(Member.is_active == True)
        )).scalars().all()

    for member in all_members:
        if member.telegram_id and member.telegram_id > 0:
            try:
                if len(full_text) > 4000:
                    parts = [full_text[i:i+4000] for i in range(0, len(full_text), 4000)]
                    for part in parts:
                        await bot.send_message(member.telegram_id, part, parse_mode="HTML")
                else:
                    await bot.send_message(member.telegram_id, full_text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send agenda to {member.telegram_id}: {e}")

    logger.info(f"Agenda sent to {len(all_members)} members for meeting {scheduled_meeting_id}")


# ─── Meeting analytics ───────────────────────────────────────────────

async def get_analytics_text() -> str:
    """Generate meeting analytics summary."""
    async with async_session() as session:
        all_tasks = (await session.execute(select(Task))).scalars().all()
        all_meetings = (await session.execute(
            select(Meeting).where(Meeting.is_confirmed == True).order_by(Meeting.date)
        )).scalars().all()

    if not all_tasks:
        return "📊 Недостаточно данных для аналитики."

    total = len(all_tasks)
    done = sum(1 for t in all_tasks if t.status == "done")
    overdue = sum(1 for t in all_tasks if t.status == "overdue")
    in_progress = sum(1 for t in all_tasks if t.status == "in_progress")
    new = sum(1 for t in all_tasks if t.status == "new")

    completion_rate = round(done / total * 100) if total else 0

    # Tasks per meeting
    tasks_per_meeting = {}
    for t in all_tasks:
        if t.meeting_id:
            tasks_per_meeting.setdefault(t.meeting_id, {"created": 0, "done": 0})
            tasks_per_meeting[t.meeting_id]["created"] += 1
            if t.status == "done":
                tasks_per_meeting[t.meeting_id]["done"] += 1

    # Average overdue days
    now = datetime.utcnow()
    overdue_days = []
    for t in all_tasks:
        if t.deadline and t.status in ("overdue", "new", "in_progress") and t.deadline < now:
            overdue_days.append((now - t.deadline).days)
    avg_overdue = round(sum(overdue_days) / len(overdue_days)) if overdue_days else 0

    # Top overdue members
    overdue_by_member: dict[str, int] = {}
    async with async_session() as session:
        result = await session.execute(
            select(Task, Member)
            .join(Member, Task.assignee_id == Member.id)
            .where(Task.status == "overdue")
        )
        for task, member in result.all():
            name = member.display_name or member.first_name or "?"
            overdue_by_member[name] = overdue_by_member.get(name, 0) + 1

    text = "📊 <b>АНАЛИТИКА</b>\n\n"

    text += "<b>Общие показатели:</b>\n"
    text += f"  📋 Всего задач: {total}\n"
    text += f"  ✅ Выполнено: {done} ({completion_rate}%)\n"
    text += f"  🔴 Просрочено: {overdue}\n"
    text += f"  🔵 В работе: {in_progress}\n"
    text += f"  ⬜ Новые: {new}\n\n"

    if avg_overdue:
        text += f"  ⏱ Среднее опоздание: {avg_overdue} дн.\n\n"

    # Per-meeting stats
    if tasks_per_meeting and all_meetings:
        text += "<b>По совещаниям:</b>\n"
        for m in all_meetings[-5:]:
            stats = tasks_per_meeting.get(m.id, {"created": 0, "done": 0})
            rate = round(stats["done"] / stats["created"] * 100) if stats["created"] else 0
            date = m.date.strftime("%d.%m")
            text += f"  📅 {date}: {stats['created']} задач, {rate}% выполнено\n"
        text += "\n"

    if overdue_by_member:
        text += "<b>Просрочки по участникам:</b>\n"
        for name, count in sorted(overdue_by_member.items(), key=lambda x: -x[1]):
            text += f"  🔴 {escape(name)}: {count}\n"

    return text
