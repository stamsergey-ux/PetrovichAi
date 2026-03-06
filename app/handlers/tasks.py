"""Task management handlers — view, complete, comment."""

import json
from datetime import datetime
from html import escape

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.database import async_session, Task, Member, Meeting, TaskComment
from app.utils import is_chairman

router = Router()


def _task_keyboard(task_id: int, status: str) -> InlineKeyboardMarkup:
    buttons = []
    if status != "done":
        buttons.append([
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"task_done:{task_id}"),
            InlineKeyboardButton(text="🔄 В работе", callback_data=f"task_progress:{task_id}"),
        ])
    buttons.append([
        InlineKeyboardButton(text="💬 Комментировать", callback_data=f"task_comment:{task_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _task_list_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if is_admin:
        buttons.append([
            InlineKeyboardButton(text="👥 Все задачи", callback_data="all_tasks"),
            InlineKeyboardButton(text="📊 Дашборд", callback_data="dashboard_cb"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def _progress_bar(done: int, total: int, length: int = 10) -> str:
    if total == 0:
        return "░" * length
    filled = round(done / total * length)
    return "▓" * filled + "░" * (length - filled)


def _format_task_card(task: Task, assignee_name: str = "—", show_assignee: bool = True) -> str:
    """Format a single task as a styled card using HTML."""
    status_icon = {
        "new": "⬜",
        "in_progress": "🔵",
        "done": "✅",
        "overdue": "🔴",
    }.get(task.status, "⬜")

    priority_badge = {
        "high": "🔥 ",
        "medium": "",
        "low": "💤 ",
    }.get(task.priority, "")

    deadline_str = ""
    if task.deadline:
        deadline_str = task.deadline.strftime("%d.%m.%Y")
        days_left = (task.deadline - datetime.utcnow()).days
        if task.status != "done":
            if days_left < 0:
                deadline_str += f" (⚠️ просрочено на {abs(days_left)} дн.)"
            elif days_left == 0:
                deadline_str += " (⚡ сегодня!)"
            elif days_left <= 2:
                deadline_str += f" (⏳ {days_left} дн.)"
    else:
        deadline_str = "без срока"

    title = escape(task.title)
    lines = [f"{status_icon} {priority_badge}<b>#{task.id}</b> {title}"]

    if show_assignee:
        lines.append(f"    👤 {escape(assignee_name)}")

    lines.append(f"    📅 {escape(deadline_str)}")

    if task.context_quote:
        quote = task.context_quote[:120]
        if len(task.context_quote) > 120:
            quote += "..."
        lines.append(f'    💭 <i>{escape(quote)}</i>')

    return "\n".join(lines)


@router.callback_query(F.data == "my_tasks")
async def cb_my_tasks(callback: CallbackQuery):
    """Show user's personal tasks."""
    user_id = callback.from_user.id
    admin = is_chairman(callback.from_user.username)

    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == user_id)
        )).scalar_one_or_none()

        if not member:
            await callback.message.answer("Ты ещё не зарегистрирован. Нажми /start")
            await callback.answer()
            return

        result = await session.execute(
            select(Task).where(
                Task.assignee_id == member.id,
                Task.status.in_(["new", "in_progress", "overdue"])
            ).order_by(Task.deadline.asc())
        )
        tasks = result.scalars().all()

    if not tasks:
        await callback.message.answer(
            "🎉 <b>Нет открытых задач!</b>\n\nВсе задачи выполнены или ещё не назначены.",
            parse_mode="HTML",
            reply_markup=_task_list_keyboard(admin),
        )
        await callback.answer()
        return

    overdue = [t for t in tasks if t.status == "overdue"]
    in_prog = [t for t in tasks if t.status == "in_progress"]
    new = [t for t in tasks if t.status == "new"]

    name = escape(member.name or member.first_name or "")
    text = f"📋 <b>Задачи: {name}</b>\n"
    text += f"<i>{len(tasks)} открытых</i>\n"

    if overdue:
        text += f"\n🚨 <b>ПРОСРОЧЕНО ({len(overdue)})</b>\n"
        for t in overdue:
            text += _format_task_card(t, show_assignee=False) + "\n\n"

    if in_prog:
        text += f"🔵 <b>В РАБОТЕ ({len(in_prog)})</b>\n"
        for t in in_prog:
            text += _format_task_card(t, show_assignee=False) + "\n\n"

    if new:
        text += f"⬜ <b>НОВЫЕ ({len(new)})</b>\n"
        for t in new:
            text += _format_task_card(t, show_assignee=False) + "\n\n"

    if len(text) > 4000:
        text = text[:4000] + "\n\n... <i>список обрезан</i>"

    keyboard = _task_keyboard(tasks[0].id, tasks[0].status) if tasks else None
    await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "all_tasks")
async def cb_all_tasks(callback: CallbackQuery):
    """Show all open tasks."""
    async with async_session() as session:
        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue"]))
            .order_by(Task.deadline.asc())
        )
        rows = result.all()

    if not rows:
        await callback.message.answer(
            "🎉 <b>Нет открытых задач!</b>",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Group by assignee
    by_assignee: dict[str, list] = {}
    for task, member in rows:
        name = (member.display_name or member.first_name or member.username) if member else "Не назначено"
        by_assignee.setdefault(name, []).append(task)

    text = f"👥 <b>Все открытые задачи</b> — {len(rows)}\n\n"

    for assignee_name, tasks in sorted(by_assignee.items()):
        overdue_cnt = sum(1 for t in tasks if t.status == "overdue")
        badge = f" 🚨{overdue_cnt}" if overdue_cnt else ""
        text += f"<b>{escape(assignee_name)}</b> ({len(tasks)}){badge}\n"
        for t in tasks:
            status_icon = {"new": "⬜", "in_progress": "🔵", "overdue": "🔴"}.get(t.status, "⬜")
            deadline = t.deadline.strftime("%d.%m") if t.deadline else "—"
            title = t.title[:55] + "..." if len(t.title) > 55 else t.title
            text += f"  {status_icon} #{t.id} {escape(title)} · {deadline}\n"
        text += "\n"

    if len(text) > 4000:
        text = text[:4000] + "\n\n... <i>список обрезан</i>"

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "dashboard_cb")
async def cb_dashboard(callback: CallbackQuery):
    """Show dashboard via callback."""
    await _send_dashboard_to(callback.message)
    await callback.answer()


async def _send_dashboard_to(message):
    """Render and send the dashboard."""
    async with async_session() as session:
        all_tasks = (await session.execute(select(Task))).scalars().all()
        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue"]))
        )
        active_rows = result.all()

    total = len(all_tasks)
    new = sum(1 for t in all_tasks if t.status == "new")
    in_progress = sum(1 for t in all_tasks if t.status == "in_progress")
    done = sum(1 for t in all_tasks if t.status == "done")

    now = datetime.utcnow()
    overdue = sum(
        1 for t in all_tasks
        if t.deadline and t.deadline < now and t.status not in ("done",)
    )

    bar = _progress_bar(done, total, 15)

    text = "📊 <b>ДАШБОРД</b>\n\n"
    text += f"Прогресс: [{bar}] {done}/{total}\n\n"
    text += f"⬜ Новые: <b>{new}</b>\n"
    text += f"🔵 В работе: <b>{in_progress}</b>\n"
    text += f"✅ Выполнено: <b>{done}</b>\n"
    text += f"🔴 Просрочено: <b>{overdue}</b>\n"

    if active_rows:
        by_person: dict[str, int] = {}
        for task, member in active_rows:
            name = (member.display_name or member.first_name) if member else "—"
            by_person[name] = by_person.get(name, 0) + 1

        text += "\n<b>Нагрузка по участникам:</b>\n"
        max_count = max(by_person.values()) if by_person else 1
        for name, count in sorted(by_person.items(), key=lambda x: -x[1]):
            mini_bar = _progress_bar(count, max_count, 8)
            text += f"  {escape(name)}: [{mini_bar}] {count}\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks"),
            InlineKeyboardButton(text="👥 Все задачи", callback_data="all_tasks"),
        ]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("task_done:"))
async def cb_task_done(callback: CallbackQuery):
    """Mark task as done."""
    task_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return

        task.status = "done"
        task.completed_at = datetime.utcnow()
        await session.commit()

    await callback.message.answer(f"✅ Задача <b>#{task_id}</b> выполнена!", parse_mode="HTML")
    await callback.answer("Выполнено!")


@router.callback_query(F.data.startswith("task_progress:"))
async def cb_task_progress(callback: CallbackQuery):
    """Mark task as in progress."""
    task_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return

        task.status = "in_progress"
        await session.commit()

    await callback.message.answer(f"🔵 Задача <b>#{task_id}</b> в работе", parse_mode="HTML")
    await callback.answer("В работе!")


@router.callback_query(F.data == "last_protocol")
async def cb_last_protocol(callback: CallbackQuery):
    """Show last confirmed meeting protocol."""
    async with async_session() as session:
        result = await session.execute(
            select(Meeting)
            .where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc())
            .limit(1)
        )
        meeting = result.scalar_one_or_none()

    if not meeting:
        await callback.message.answer(
            "📭 <b>Пока нет сохранённых протоколов.</b>",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    date_str = meeting.date.strftime('%d.%m.%Y')
    title = escape(meeting.title or "Без названия")
    participants = escape(meeting.participants or "—")
    summary = escape(meeting.summary or "—")

    text = f"📝 <b>ПРОТОКОЛ</b>\n\n"
    text += f"<b>{title}</b>\n"
    text += f"📅 {date_str}\n"
    text += f"👥 {participants}\n\n"
    text += f"<b>Краткое содержание:</b>\n{summary}\n"

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

    await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()
