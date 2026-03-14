"""Task management handlers — view, complete, comment."""

import json
from datetime import datetime
from html import escape

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, delete as sa_delete

from app.database import async_session, Task, Member, Meeting, TaskComment
from app.utils import is_chairman

router = Router()


class TaskCompletion(StatesGroup):
    waiting_comment = State()


class BulkSelection(StatesGroup):
    selecting = State()


# Status icons including pending_done
STATUS_ICON = {
    "new": "⬜",
    "in_progress": "🔵",
    "done": "✅",
    "overdue": "⏰",
    "pending_done": "🔍",
}


def _task_keyboard(task_id: int, status: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if status == "pending_done":
        buttons.append([
            InlineKeyboardButton(text="🟡 Ожидает подтверждения председателя", callback_data="noop"),
        ])
    elif status != "done":
        buttons.append([
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"task_done:{task_id}"),
            InlineKeyboardButton(text="🔄 В работе", callback_data=f"task_progress:{task_id}"),
        ])
    buttons.append([
        InlineKeyboardButton(text="💬 Комментировать", callback_data=f"task_comment:{task_id}"),
    ])
    if is_admin:
        buttons.append([
            InlineKeyboardButton(text="🗑 Удалить задачу", callback_data=f"task_delete:{task_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _task_list_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup | None:
    buttons = []
    if is_admin:
        buttons.append([
            InlineKeyboardButton(text="📋 Мои поручения", callback_data="my_assigned"),
            InlineKeyboardButton(text="👥 Все задачи", callback_data="all_tasks"),
        ])
        buttons.append([
            InlineKeyboardButton(text="📊 Дашборд", callback_data="dashboard_cb"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def _task_buttons(
    task_rows: list,
    show_assignee: bool = False,
) -> list[list[InlineKeyboardButton]]:
    """One clickable button per task: icon + id + optional assignee + truncated title."""
    rows = []
    for item in task_rows:
        if isinstance(item, Task):
            task, member = item, None
        else:
            # SQLAlchemy Row from select(Task, Member)
            task, member = item[0], item[1]

        icon = STATUS_ICON.get(task.status, "⬜")

        if show_assignee:
            full_name = (member.name or member.first_name or member.username or "—").strip() if member else "—"
            parts = full_name.split()
            short_name = parts[-1] if len(parts) > 1 else full_name
            btn_text = f"{icon} {short_name} — {task.title}"
        else:
            btn_text = f"{icon} {task.title}"
        if len(btn_text) > 60:
            btn_text = btn_text[:59] + "…"

        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"task_detail:{task.id}")])
    return rows


def _progress_bar(done: int, total: int, length: int = 10) -> str:
    if total == 0:
        return "░" * length
    filled = round(done / total * length)
    return "▓" * filled + "░" * (length - filled)


def _format_task_card(
    task: Task,
    assignee_name: str = "—",
    show_assignee: bool = True,
    full: bool = False,
    creator_name: str | None = None,
    meeting_title: str | None = None,
) -> str:
    status_icon = STATUS_ICON.get(task.status, "⬜")
    priority_badge = {"high": "🔥 ", "medium": "", "low": "💤 "}.get(task.priority, "")

    deadline_str = ""
    if task.deadline:
        deadline_str = task.deadline.strftime("%d.%m.%Y")
        days_left = (task.deadline - datetime.utcnow()).days
        if task.status not in ("done", "pending_done"):
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
        lines.append(f"👤 Исполнитель: {escape(assignee_name)}")
    lines.append(f"📅 Срок: {escape(deadline_str)}")

    if full:
        if creator_name:
            lines.append(f"📌 Поставил: {escape(creator_name)}")
        if meeting_title:
            lines.append(f"📋 Протокол: {escape(meeting_title)}")
        if task.description:
            lines.append(f"\n📝 <b>Описание:</b>\n{escape(task.description)}")
        if task.context_quote:
            lines.append(f'\n💭 <b>Контекст из протокола:</b>\n<i>{escape(task.context_quote)}</i>')
        if task.completion_comment:
            lines.append(f"\n✅ <b>Как выполнено:</b>\n{escape(task.completion_comment)}")
    else:
        if task.context_quote:
            quote = task.context_quote[:120]
            if len(task.context_quote) > 120:
                quote += "..."
            lines.append(f'💭 <i>{escape(quote)}</i>')

    return "\n".join(lines)


@router.callback_query(F.data == "my_tasks")
async def cb_my_tasks(callback: CallbackQuery):
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
                Task.status.in_(["new", "in_progress", "overdue", "pending_done"])
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

    name = escape(member.name or member.first_name or "")
    overdue_cnt = sum(1 for t in tasks if t.status == "overdue")
    pending_cnt = sum(1 for t in tasks if t.status == "pending_done")
    badges = []
    if overdue_cnt:
        badges.append(f"🚨 {overdue_cnt} просрочено")
    if pending_cnt:
        badges.append(f"🟡 {pending_cnt} ждут подтверждения")
    badge_str = ("\n" + " · ".join(badges)) if badges else ""

    text = f"📋 <b>Задачи: {name}</b>\n{len(tasks)} открытых{badge_str}"

    task_rows = _task_buttons(tasks)
    admin_kb = _task_list_keyboard(admin)
    admin_rows = admin_kb.inline_keyboard if admin_kb else []
    keyboard = InlineKeyboardMarkup(inline_keyboard=task_rows + admin_rows)

    await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


# ── All tasks: grouped by meeting ────────────────────────────────────────────

@router.callback_query(F.data == "all_tasks")
async def cb_all_tasks(callback: CallbackQuery):
    """Show list of meetings with open task counts — newest first."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Task, Meeting)
                .outerjoin(Meeting, Task.meeting_id == Meeting.id)
                .where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
            )
            rows = result.all()

        if not rows:
            await callback.message.answer("🎉 <b>Нет открытых задач!</b>", parse_mode="HTML")
            await callback.answer()
            return

        # Group: meeting_id -> (meeting_obj, count, overdue_count)
        groups: dict[int, list] = {}  # mid -> [meeting, total, overdue]
        for row in rows:
            task, meeting = row[0], row[1]
            mid = task.meeting_id or 0
            if mid not in groups:
                groups[mid] = [meeting, 0, 0]
            groups[mid][1] += 1
            if task.status == "overdue":
                groups[mid][2] += 1

        # Sort: meetings by date desc, "no meeting" group last
        def sort_key(item):
            mid, (meeting, *_) = item
            if mid == 0:
                return (0,)  # last
            return (1, -(meeting.date.timestamp() if meeting and meeting.date else 0))

        sorted_groups = sorted(groups.items(), key=sort_key, reverse=False)

        total = len(rows)
        overdue_total = sum(1 for row in rows if row[0].status == "overdue")

        text = f"👥 <b>Все открытые задачи — {total}</b>"
        if overdue_total:
            text += f"\n🚨 {overdue_total} просрочено"
        text += "\n\nВыбери протокол:"

        btn_rows = []
        for mid, (meeting, cnt, ov) in sorted_groups:
            if mid == 0:
                label = "📌 Без протокола"
            else:
                date_str = meeting.date.strftime("%d.%m.%Y") if meeting and meeting.date else "—"
                title = (meeting.title or "Совещание")[:30]
                label = f"📋 {date_str} — {title}"

            badge = f"  {cnt} зад."
            if ov:
                badge += f" 🚨{ov}"
            btn_text = label + badge
            if len(btn_text) > 60:
                btn_text = btn_text[:59] + "…"

            btn_rows.append([
                InlineKeyboardButton(text=btn_text, callback_data=f"tasks_by_meeting:{mid}")
            ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=btn_rows)
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(F.data.startswith("tasks_by_meeting:"))
async def cb_tasks_by_meeting(callback: CallbackQuery):
    """Show open tasks for a specific meeting (or tasks without meeting)."""
    try:
        mid = int(callback.data.split(":")[1])

        async with async_session() as session:
            if mid == 0:
                result = await session.execute(
                    select(Task, Member)
                    .outerjoin(Member, Task.assignee_id == Member.id)
                    .where(
                        Task.status.in_(["new", "in_progress", "overdue", "pending_done"]),
                        Task.meeting_id == None,
                    )
                    .order_by(Member.display_name.asc().nulls_last(), Task.deadline.asc().nulls_last())
                )
                header = "📌 <b>Без протокола</b>"
            else:
                meeting = await session.get(Meeting, mid)
                result = await session.execute(
                    select(Task, Member)
                    .outerjoin(Member, Task.assignee_id == Member.id)
                    .where(
                        Task.status.in_(["new", "in_progress", "overdue", "pending_done"]),
                        Task.meeting_id == mid,
                    )
                    .order_by(Member.display_name.asc().nulls_last(), Task.deadline.asc().nulls_last())
                )
                if meeting:
                    date_str = meeting.date.strftime("%d.%m.%Y")
                    header = f"📋 <b>{escape(meeting.title or 'Совещание')}</b>\n📅 {date_str}"
                else:
                    header = "📋 <b>Протокол</b>"
            rows = result.all()

        if not rows:
            await callback.message.answer(
                f"{header}\n\n🎉 Нет открытых задач", parse_mode="HTML"
            )
            await callback.answer()
            return

        overdue = sum(1 for row in rows if row[0].status == "overdue")
        text = f"{header}\n\n{len(rows)} открытых задач"
        if overdue:
            text += f"  🚨 {overdue} просрочено"

        task_rows = _task_buttons(rows, show_assignee=True)
        back_row = [InlineKeyboardButton(text="← Все протоколы", callback_data="all_tasks")]
        extra_rows = []
        if is_chairman(callback.from_user.username):
            extra_rows = [[InlineKeyboardButton(text="☑ Выбрать несколько", callback_data=f"bulk_mode:{mid}")]]
        keyboard = InlineKeyboardMarkup(inline_keyboard=task_rows + extra_rows + [back_row])

        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


# ── Bulk selection helpers ────────────────────────────────────────────────────

async def _fetch_open_tasks_for_meeting(mid: int):
    """Return list of (Task, Member|None) rows for open tasks in a meeting."""
    async with async_session() as session:
        if mid == 0:
            result = await session.execute(
                select(Task, Member)
                .outerjoin(Member, Task.assignee_id == Member.id)
                .where(
                    Task.status.in_(["new", "in_progress", "overdue", "pending_done"]),
                    Task.meeting_id == None,
                )
                .order_by(Member.display_name.asc().nulls_last(), Task.deadline.asc().nulls_last())
            )
        else:
            result = await session.execute(
                select(Task, Member)
                .outerjoin(Member, Task.assignee_id == Member.id)
                .where(
                    Task.status.in_(["new", "in_progress", "overdue", "pending_done"]),
                    Task.meeting_id == mid,
                )
                .order_by(Member.display_name.asc().nulls_last(), Task.deadline.asc().nulls_last())
            )
        return result.all()


def _bulk_keyboard(rows, selected_ids: set, mid: int) -> InlineKeyboardMarkup:
    """Keyboard with per-task checkboxes and action buttons."""
    btn_rows = []
    for row in rows:
        task, member = row[0], row[1]
        icon = "☑" if task.id in selected_ids else "☐"
        full_name = (member.name or "—").strip() if member else "—"
        parts = full_name.split()
        short_name = parts[-1] if len(parts) > 1 else full_name
        btn_text = f"{icon} {short_name} — {task.title}"
        if len(btn_text) > 60:
            btn_text = btn_text[:59] + "…"
        btn_rows.append([InlineKeyboardButton(
            text=btn_text,
            callback_data=f"bulk_toggle:{task.id}:{mid}"
        )])

    n = len(selected_ids)
    action_row = [
        InlineKeyboardButton(text=f"🗑 Удалить ({n})", callback_data=f"bulk_delete:{mid}"),
        InlineKeyboardButton(text=f"✅ Принять ({n})", callback_data=f"bulk_confirm:{mid}"),
    ]
    control_row = [
        InlineKeyboardButton(text="☑ Все", callback_data=f"bulk_all:{mid}"),
        InlineKeyboardButton(text="☐ Снять", callback_data=f"bulk_none:{mid}"),
        InlineKeyboardButton(text="✖ Отмена", callback_data=f"bulk_cancel:{mid}"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=btn_rows + [action_row, control_row])


@router.callback_query(F.data.startswith("bulk_mode:"))
async def cb_bulk_mode(callback: CallbackQuery, state: FSMContext):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return
    try:
        mid = int(callback.data.split(":")[1])
        rows = await _fetch_open_tasks_for_meeting(mid)
        if not rows:
            await callback.answer("Нет открытых задач", show_alert=True)
            return

        await state.set_state(BulkSelection.selecting)
        await state.update_data(mid=mid, selected=[])

        keyboard = _bulk_keyboard(rows, set(), mid)
        await callback.message.answer(
            "☑ <b>Режим выбора</b> — выбери задачи для действия:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(BulkSelection.selecting, F.data.startswith("bulk_toggle:"))
async def cb_bulk_toggle(callback: CallbackQuery, state: FSMContext):
    try:
        _, task_id_str, mid_str = callback.data.split(":")
        task_id = int(task_id_str)
        mid = int(mid_str)

        data = await state.get_data()
        selected = set(data.get("selected", []))
        if task_id in selected:
            selected.discard(task_id)
        else:
            selected.add(task_id)
        await state.update_data(selected=list(selected))

        rows = await _fetch_open_tasks_for_meeting(mid)
        keyboard = _bulk_keyboard(rows, selected, mid)
        await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(BulkSelection.selecting, F.data.startswith("bulk_all:"))
async def cb_bulk_all(callback: CallbackQuery, state: FSMContext):
    try:
        mid = int(callback.data.split(":")[1])
        rows = await _fetch_open_tasks_for_meeting(mid)
        selected = {row[0].id for row in rows}
        await state.update_data(selected=list(selected))

        keyboard = _bulk_keyboard(rows, selected, mid)
        await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer(f"Выбрано {len(selected)}")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(BulkSelection.selecting, F.data.startswith("bulk_none:"))
async def cb_bulk_none(callback: CallbackQuery, state: FSMContext):
    try:
        mid = int(callback.data.split(":")[1])
        rows = await _fetch_open_tasks_for_meeting(mid)
        await state.update_data(selected=[])

        keyboard = _bulk_keyboard(rows, set(), mid)
        await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer("Выделение снято")
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(BulkSelection.selecting, F.data.startswith("bulk_cancel:"))
async def cb_bulk_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer("Отменено")


@router.callback_query(BulkSelection.selecting, F.data.startswith("bulk_delete:"))
async def cb_bulk_delete(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return
    try:
        data = await state.get_data()
        selected = list(data.get("selected", []))
        if not selected:
            await callback.answer("Не выбрано ни одной задачи", show_alert=True)
            return

        async with async_session() as session:
            tasks_result = await session.execute(
                select(Task, Member)
                .outerjoin(Member, Task.assignee_id == Member.id)
                .where(Task.id.in_(selected))
            )
            task_rows = tasks_result.all()

            await session.execute(sa_delete(TaskComment).where(TaskComment.task_id.in_(selected)))
            await session.execute(sa_delete(Task).where(Task.id.in_(selected)))
            await session.commit()

        await state.clear()
        await callback.message.delete()
        await callback.message.answer(
            f"🗑 <b>Удалено {len(selected)} задач</b>",
            parse_mode="HTML",
        )
        await callback.answer(f"Удалено {len(selected)}")

        notified = set()
        for row in task_rows:
            task, member = row[0], row[1]
            if member and member.telegram_id and member.telegram_id > 0 and member.telegram_id not in notified:
                notified.add(member.telegram_id)
                try:
                    await bot.send_message(
                        member.telegram_id,
                        f"🗑 <b>Председатель удалил несколько задач</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(BulkSelection.selecting, F.data.startswith("bulk_confirm:"))
async def cb_bulk_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return
    try:
        data = await state.get_data()
        selected = list(data.get("selected", []))
        if not selected:
            await callback.answer("Не выбрано ни одной задачи", show_alert=True)
            return

        now = datetime.utcnow()
        async with async_session() as session:
            tasks_result = await session.execute(
                select(Task, Member)
                .outerjoin(Member, Task.assignee_id == Member.id)
                .where(Task.id.in_(selected))
            )
            task_rows = tasks_result.all()

            for row in task_rows:
                task = row[0]
                task.status = "done"
                task.completed_at = now
                session.add(task)
            await session.commit()

        await state.clear()
        await callback.message.delete()
        await callback.message.answer(
            f"✅ <b>Принято {len(selected)} задач</b>",
            parse_mode="HTML",
        )
        await callback.answer(f"Принято {len(selected)}")

        notified = set()
        for row in task_rows:
            task, member = row[0], row[1]
            if member and member.telegram_id and member.telegram_id > 0 and member.telegram_id not in notified:
                notified.add(member.telegram_id)
                try:
                    await bot.send_message(
                        member.telegram_id,
                        f"✅ <b>Председатель подтвердил выполнение задач</b>\n\n"
                        f"<i>Несколько задач отмечены как выполненные.</i>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(F.data.startswith("task_detail:"))
async def cb_task_detail(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.", show_alert=True)
            return

        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None
        creator = await session.get(Member, task.created_by_id) if task.created_by_id else None
        meeting = await session.get(Meeting, task.meeting_id) if task.meeting_id else None

    assignee_name = assignee.name if assignee else "Не назначено"
    creator_name = creator.name if creator else None
    meeting_title = (meeting.title or f"Совещание {meeting.date.strftime('%d.%m.%Y')}") if meeting else None

    text = f"📋 <b>Задача #{task_id}</b>\n\n"
    text += _format_task_card(
        task,
        assignee_name=assignee_name,
        show_assignee=True,
        full=True,
        creator_name=creator_name,
        meeting_title=meeting_title,
    )

    admin = is_chairman(callback.from_user.username)
    is_assignee = assignee and assignee.telegram_id == callback.from_user.id
    keyboard = _task_keyboard(task_id, task.status, is_admin=admin) if (admin or is_assignee) else None

    await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "my_assigned")
async def cb_my_assigned(callback: CallbackQuery):
    try:
        if not is_chairman(callback.from_user.username):
            await callback.answer("⛔ Только для председателя", show_alert=True)
            return

        async with async_session() as session:
            member = (await session.execute(
                select(Member).where(Member.telegram_id == callback.from_user.id)
            )).scalar_one_or_none()

            if not member:
                await callback.answer("Ты не зарегистрирован. Нажми /start")
                return

            result = await session.execute(
                select(Task, Member)
                .outerjoin(Member, Task.assignee_id == Member.id)
                .where(Task.created_by_id == member.id)
                .order_by(Task.created_at.desc())
                .limit(30)
            )
            rows = result.all()

        if not rows:
            await callback.message.answer(
                "📭 <b>Нет поручений.</b>\n\nЗадачи, поставленные тобой, появятся здесь.",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        pending_cnt = sum(1 for row in rows if row[0].status == "pending_done")
        overdue_cnt = sum(1 for row in rows if row[0].status == "overdue")
        badges = []
        if pending_cnt:
            badges.append(f"🟡 {pending_cnt} ждут подтверждения")
        if overdue_cnt:
            badges.append(f"🚨 {overdue_cnt} просрочено")
        badge_str = ("\n" + " · ".join(badges)) if badges else ""

        text = f"📋 <b>Мои поручения</b> — {len(rows)} задач{badge_str}"

        task_rows = _task_buttons(rows, show_assignee=True)
        keyboard = InlineKeyboardMarkup(inline_keyboard=task_rows) if task_rows else None

        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}"[:200], show_alert=True)


@router.callback_query(F.data == "dashboard_cb")
async def cb_dashboard(callback: CallbackQuery):
    await _send_dashboard_to(callback.message)
    await callback.answer()


async def _send_dashboard_to(message):
    async with async_session() as session:
        all_tasks = (await session.execute(select(Task))).scalars().all()
        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.status.in_(["new", "in_progress", "overdue", "pending_done"]))
        )
        active_rows = result.all()

    total = len(all_tasks)
    new = sum(1 for t in all_tasks if t.status == "new")
    in_progress = sum(1 for t in all_tasks if t.status == "in_progress")
    done = sum(1 for t in all_tasks if t.status == "done")
    pending = sum(1 for t in all_tasks if t.status == "pending_done")

    now = datetime.utcnow()
    overdue = sum(
        1 for t in all_tasks
        if t.deadline and t.deadline < now and t.status not in ("done", "pending_done")
    )

    bar = _progress_bar(done, total, 15)
    text = "📊 <b>ДАШБОРД</b>\n\n"
    text += f"Прогресс: [{bar}] {done}/{total}\n\n"
    text += f"⬜ Новые: <b>{new}</b>\n"
    text += f"🔵 В работе: <b>{in_progress}</b>\n"
    text += f"🟡 Ожидают подтверждения: <b>{pending}</b>\n"
    text += f"✅ Выполнено: <b>{done}</b>\n"
    text += f"🔴 Просрочено: <b>{overdue}</b>\n"

    if active_rows:
        by_person: dict[str, int] = {}
        for row in active_rows:
            task, member = row[0], row[1]
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


# ── Mark done: ask for completion comment ──────────────────────────────────────

@router.callback_query(F.data.startswith("task_done:"))
async def cb_task_done(callback: CallbackQuery, state: FSMContext):
    """Executor marks task done — ask for a short completion comment."""
    task_id = int(callback.data.split(":")[1])
    admin = is_chairman(callback.from_user.username)

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return
        if not admin:
            member = (await session.execute(
                select(Member).where(Member.telegram_id == callback.from_user.id)
            )).scalar_one_or_none()
            if not member or task.assignee_id != member.id:
                await callback.answer("⛔ Только исполнитель задачи может отметить выполнение.", show_alert=True)
                return

    await state.set_state(TaskCompletion.waiting_comment)
    await state.update_data(task_id=task_id)
    await callback.answer()
    await callback.message.answer(
        f"✅ Задача <b>#{task_id}</b> — расскажи коротко, как выполнена.\n\n"
        f"Голосом или текстом (1-2 предложения):",
        parse_mode="HTML",
    )


@router.message(TaskCompletion.waiting_comment, F.text)
async def process_done_text(message: Message, state: FSMContext, bot: Bot):
    await _save_and_notify(message, state, bot, message.text)


@router.message(TaskCompletion.waiting_comment, F.voice)
async def process_done_voice(message: Message, state: FSMContext, bot: Bot):
    await message.answer("🎙 Распознаю...")
    try:
        from app.voice import transcribe_voice
        file = await bot.download(message.voice)
        text = await transcribe_voice(file.read(), ".ogg")
        if not text:
            await message.answer("⚠️ Не удалось распознать. Напиши текстом.")
            return
        await message.answer(f"📝 <i>{escape(text)}</i>", parse_mode="HTML")
        await _save_and_notify(message, state, bot, text)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def _save_and_notify(message: Message, state: FSMContext, bot: Bot, comment: str):
    data = await state.get_data()
    task_id = data["task_id"]
    await state.clear()

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await message.answer("Задача не найдена.")
            return
        task.status = "pending_done"
        task.completion_comment = comment
        await session.commit()

        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None
        assignee_name = assignee.name if assignee else "—"

        # Load all chairmen
        chairmen = (await session.execute(
            select(Member).where(Member.is_chairman == True)
        )).scalars().all()

    await message.answer(
        f"🟡 <b>Готово!</b> Задача #{task_id} отправлена председателю на подтверждение.",
        parse_mode="HTML",
    )

    # Notify all chairmen
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить выполнение", callback_data=f"done_confirm:{task_id}"),
            InlineKeyboardButton(text="↩️ Вернуть в работу", callback_data=f"done_return:{task_id}"),
        ]
    ])
    for ch in chairmen:
        if ch.telegram_id and ch.telegram_id > 0:
            try:
                await bot.send_message(
                    ch.telegram_id,
                    f"🟡 <b>Задача выполнена — требует подтверждения</b>\n\n"
                    f"<b>#{task_id}</b> {escape(task.title)}\n"
                    f"👤 Исполнитель: {escape(assignee_name)}\n\n"
                    f"💬 <b>Как выполнено:</b>\n{escape(comment)}",
                    parse_mode="HTML",
                    reply_markup=confirm_kb,
                )
            except Exception:
                pass


# ── Chairman confirms or returns task ─────────────────────────────────────────

@router.callback_query(F.data.startswith("done_confirm:"))
async def cb_confirm_done(callback: CallbackQuery, bot: Bot):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return
        task.status = "done"
        task.completed_at = datetime.utcnow()
        await session.commit()
        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None

    await callback.answer("✅ Подтверждено!")
    await callback.message.answer(
        f"✅ <b>Задача #{task_id} закрыта!</b>\n{escape(task.title)}",
        parse_mode="HTML",
    )

    # Notify assignee
    if assignee and assignee.telegram_id and assignee.telegram_id > 0:
        try:
            await bot.send_message(
                assignee.telegram_id,
                f"✅ <b>Председатель подтвердил выполнение задачи #{task_id}</b>\n\n"
                f"{escape(task.title)}\n\n<i>Задача закрыта.</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("done_return:"))
async def cb_return_task(callback: CallbackQuery, bot: Bot):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return
        task.status = "in_progress"
        task.completion_comment = None
        await session.commit()
        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None

    await callback.answer("↩️ Возвращено в работу")
    await callback.message.answer(
        f"↩️ <b>Задача #{task_id} возвращена в работу</b>",
        parse_mode="HTML",
    )

    if assignee and assignee.telegram_id and assignee.telegram_id > 0:
        try:
            await bot.send_message(
                assignee.telegram_id,
                f"↩️ <b>Председатель вернул задачу #{task_id} в работу</b>\n\n"
                f"{escape(task.title)}\n\n<i>Уточни детали и выполни повторно.</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── noop for disabled buttons ─────────────────────────────────────────────────

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


# ── Delete task (chairman only) ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("task_delete:"))
async def cb_task_delete(callback: CallbackQuery):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.", show_alert=True)
            return
        title = task.title[:60]

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"task_delete_confirm:{task_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="noop"),
        ]
    ])
    await callback.message.answer(
        f"🗑 <b>Удалить задачу #{task_id}?</b>\n\n<i>{escape(title)}</i>\n\nЭто действие нельзя отменить.",
        parse_mode="HTML",
        reply_markup=confirm_kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("task_delete_confirm:"))
async def cb_task_delete_confirm(callback: CallbackQuery, bot: Bot):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача уже удалена.", show_alert=True)
            return

        title = task.title
        assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None

        # Delete comments first, then task
        await session.execute(sa_delete(TaskComment).where(TaskComment.task_id == task_id))
        await session.delete(task)
        await session.commit()

    await callback.answer("🗑 Задача удалена")
    await callback.message.answer(
        f"🗑 <b>Задача #{task_id} удалена</b>\n<i>{escape(title)}</i>",
        parse_mode="HTML",
    )

    # Notify assignee
    if assignee and assignee.telegram_id and assignee.telegram_id > 0:
        try:
            await bot.send_message(
                assignee.telegram_id,
                f"🗑 <b>Задача #{task_id} была удалена председателем</b>\n\n<i>{escape(title)}</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── In progress ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("task_progress:"))
async def cb_task_progress(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    admin = is_chairman(callback.from_user.username)

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return
        if not admin:
            member = (await session.execute(
                select(Member).where(Member.telegram_id == callback.from_user.id)
            )).scalar_one_or_none()
            if not member or task.assignee_id != member.id:
                await callback.answer("⛔ Только исполнитель задачи может менять статус.", show_alert=True)
                return
        task.status = "in_progress"
        await session.commit()

    await callback.message.answer(f"🔵 Задача <b>#{task_id}</b> в работе", parse_mode="HTML")
    await callback.answer("В работе!")


@router.callback_query(F.data == "last_protocol")
async def cb_last_protocol(callback: CallbackQuery):
    async with async_session() as session:
        result = await session.execute(
            select(Meeting)
            .where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc())
            .limit(1)
        )
        meeting = result.scalar_one_or_none()

    if not meeting:
        await callback.message.answer("📭 <b>Пока нет сохранённых протоколов.</b>", parse_mode="HTML")
        await callback.answer()
        return

    date_str = meeting.date.strftime('%d.%m.%Y')
    title = escape(meeting.title or "Без названия")
    participants = escape(meeting.participants or "—")
    summary = escape(meeting.summary or "—")

    text = f"📝 <b>ПРОТОКОЛ</b>\n\n<b>{title}</b>\n📅 {date_str}\n👥 {participants}\n\n<b>Краткое содержание:</b>\n{summary}\n"

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
