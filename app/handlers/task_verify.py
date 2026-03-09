"""Task verification flow — chairman assigns exact executor and deadline."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from html import escape

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message,
)
from sqlalchemy import select, delete as sa_delete

from app.database import async_session, Task, Member
from app.utils import is_chairman

logger = logging.getLogger(__name__)
router = Router()


class VerifyState(StatesGroup):
    waiting_custom_deadline = State()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_unverified_tasks() -> list[Task]:
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.is_verified == False)
            .order_by(Task.created_at.asc())
        )
        return result.scalars().all()


async def _get_active_members() -> list[Member]:
    async with async_session() as session:
        result = await session.execute(
            select(Member).where(Member.is_active == True).order_by(Member.display_name)
        )
        return result.scalars().all()


def _member_keyboard(members: list[Member], task_id: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for m in members:
        name = (m.display_name or m.first_name or m.username or f"ID{m.telegram_id}")[:18]
        row.append(InlineKeyboardButton(
            text=name,
            callback_data=f"vt_a:{task_id}:{m.id}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🗑 Удалить задачу", callback_data=f"vt_del:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _deadline_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 неделя", callback_data=f"vt_dl:{task_id}:7"),
            InlineKeyboardButton(text="2 недели", callback_data=f"vt_dl:{task_id}:14"),
        ],
        [
            InlineKeyboardButton(text="1 месяц", callback_data=f"vt_dl:{task_id}:30"),
            InlineKeyboardButton(text="3 месяца", callback_data=f"vt_dl:{task_id}:90"),
        ],
        [
            InlineKeyboardButton(text="✏️ Ввести дату вручную", callback_data=f"vt_dl_custom:{task_id}"),
        ],
        [
            InlineKeyboardButton(text="⏭ Без срока", callback_data=f"vt_dl:{task_id}:0"),
        ],
    ])


async def _show_task(target: Message | CallbackQuery, task: Task, total: int, index: int):
    """Show one task for verification. If AI already filled assignee+deadline — show confirm card."""
    msg = target if isinstance(target, Message) else target.message

    ai_hint = ""
    if task.description and "Ответственный (из транскрипта):" in task.description:
        hint = task.description.split("Ответственный (из транскрипта):")[-1].strip()
        if hint and hint != "не определён":
            ai_hint = hint

    header = f"📋 <b>ВЕРИФИКАЦИЯ ЗАДАЧ</b> — {index} из {total}\n\n📌 <b>{escape(task.title)}</b>\n"
    if task.context_quote:
        header += f"💬 <i>«{escape(task.context_quote[:200])}»</i>\n"

    # Case 1: AI already identified assignee AND deadline — show confirm card
    if task.assignee_id and task.deadline:
        async with async_session() as session:
            assignee = await session.get(Member, task.assignee_id)
        assignee_name = (assignee.display_name or assignee.first_name or assignee.username) if assignee else "?"
        deadline_str = task.deadline.strftime("%d.%m.%Y")

        text = header
        text += f"\n👤 <b>Исполнитель:</b> {escape(assignee_name)}"
        if ai_hint:
            text += f" <i>(из транскрипта: {escape(ai_hint)})</i>"
        text += f"\n📅 <b>Срок:</b> {deadline_str}\n\n<i>Всё верно?</i>"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"vt_ok:{task.id}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"vt_change:{task.id}"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить задачу", callback_data=f"vt_del:{task.id}")],
        ])
        await msg.answer(text, parse_mode="HTML", reply_markup=keyboard)
        return

    # Case 2: Assignee identified but no deadline — skip member selection
    if task.assignee_id and not task.deadline:
        async with async_session() as session:
            assignee = await session.get(Member, task.assignee_id)
        assignee_name = (assignee.display_name or assignee.first_name or assignee.username) if assignee else "?"

        text = header
        text += f"\n👤 <b>Исполнитель:</b> {escape(assignee_name)}"
        if ai_hint:
            text += f" <i>(из транскрипта: {escape(ai_hint)})</i>"
        text += "\n\n📅 Выбери срок выполнения:"

        await msg.answer(text, parse_mode="HTML", reply_markup=_deadline_keyboard(task.id))
        return

    # Case 3: No assignee — show member selection grid
    members = await _get_active_members()
    text = header
    if ai_hint:
        text += f"\n🤖 AI предложил исполнителя: <i>{escape(ai_hint)}</i>"
    text += "\n\n👤 Выбери исполнителя:"

    await msg.answer(text, parse_mode="HTML", reply_markup=_member_keyboard(members, task.id))


# ── Entry point ────────────────────────────────────────────────────────────────

async def start_verification(message: Message, user=None):
    """Called from chat dispatcher or inline button — start the verification flow."""
    check_user = user or message.from_user
    if not is_chairman(check_user.username):
        await message.answer("⛔ Верификация задач доступна только администраторам.")
        return

    tasks = await _get_unverified_tasks()
    if not tasks:
        await message.answer("✅ Все задачи верифицированы. Нет задач, ожидающих подтверждения.")
        return

    await message.answer(
        f"🔍 <b>Найдено задач без верификации: {len(tasks)}</b>\n\n"
        f"Для каждой задачи нужно назначить точного исполнителя и срок.\n"
        f"После этого задача попадёт в реестр.",
        parse_mode="HTML",
    )
    await _show_task(message, tasks[0], len(tasks), 1)


# ── Confirm pre-filled task (assignee + deadline already set by AI) ────────────

@router.callback_query(F.data.startswith("vt_ok:"))
async def cb_confirm_prefilled(callback: CallbackQuery, bot: Bot):
    """Chairman confirms AI-extracted assignee and deadline — mark as verified."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return
        task.is_verified = True
        await session.commit()
        task_title = task.title
        deadline = task.deadline
        assignee_id = task.assignee_id
        assignee = await session.get(Member, assignee_id) if assignee_id else None

    deadline_str = deadline.strftime("%d.%m.%Y") if deadline else "без срока"
    await callback.answer("✅ Верифицировано!")
    await callback.message.answer(
        f"✅ <b>Задача верифицирована!</b>\n"
        f"📌 {escape(task_title)}\n"
        f"📅 {deadline_str}",
        parse_mode="HTML",
    )
    await _notify_assignee(bot, task_id, task_title, assignee, deadline_str)
    await _show_next(callback)


@router.callback_query(F.data.startswith("vt_change:"))
async def cb_change_assignee(callback: CallbackQuery):
    """Chairman wants to change the AI-suggested assignee — show member grid."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    members = await _get_active_members()
    await callback.answer()
    await callback.message.answer(
        "👤 Выбери нового исполнителя:",
        reply_markup=_member_keyboard(members, task_id),
    )


# ── Step 1: Assign member ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("vt_a:"))
async def cb_assign(callback: CallbackQuery):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return

    parts = callback.data.split(":")
    task_id, member_id = int(parts[1]), int(parts[2])

    async with async_session() as session:
        task = await session.get(Task, task_id)
        member = await session.get(Member, member_id)
        if not task or not member:
            await callback.answer("Задача или участник не найдены.")
            return
        task.assignee_id = member_id
        await session.commit()
        task_title = task.title
        member_name = member.display_name or member.first_name or member.username

    await callback.answer(f"👤 {member_name}")
    await callback.message.answer(
        f"📌 <b>{escape(task_title)}</b>\n"
        f"👤 Исполнитель: <b>{escape(member_name)}</b> ✅\n\n"
        f"📅 Выбери срок выполнения:",
        parse_mode="HTML",
        reply_markup=_deadline_keyboard(task_id),
    )


# ── Step 2a: Deadline via button ───────────────────────────────────────────────

@router.callback_query(F.data.startswith("vt_dl:"))
async def cb_deadline(callback: CallbackQuery, bot: Bot):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return

    parts = callback.data.split(":")
    task_id, days = int(parts[1]), int(parts[2])

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена.")
            return
        if days > 0:
            task.deadline = datetime.utcnow() + timedelta(days=days)
        task.is_verified = True
        await session.commit()
        task_title = task.title
        deadline = task.deadline
        assignee_id = task.assignee_id

        assignee = await session.get(Member, assignee_id) if assignee_id else None

    deadline_str = deadline.strftime("%d.%m.%Y") if deadline else "без срока"
    await callback.answer("✅ Верифицировано!")
    await callback.message.answer(
        f"✅ <b>Задача верифицирована!</b>\n"
        f"📌 {escape(task_title)}\n"
        f"📅 {deadline_str}",
        parse_mode="HTML",
    )

    # Notify assignee
    await _notify_assignee(bot, task_id, task_title, assignee, deadline_str)
    await _show_next(callback)


# ── Step 2b: Custom deadline ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("vt_dl_custom:"))
async def cb_deadline_custom(callback: CallbackQuery, state: FSMContext):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    await state.set_state(VerifyState.waiting_custom_deadline)
    await state.update_data(task_id=task_id)
    await callback.answer()
    await callback.message.answer(
        "✏️ Введи дату в формате <b>ДД.ММ.ГГГГ</b>\nПример: <i>31.03.2026</i>",
        parse_mode="HTML",
    )


@router.message(VerifyState.waiting_custom_deadline)
async def process_custom_deadline(message: Message, state: FSMContext):
    if not is_chairman(message.from_user.username):
        await state.clear()
        return

    data = await state.get_data()
    task_id = data.get("task_id")

    try:
        deadline = datetime.strptime(message.text.strip(), "%d.%m.%Y")
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введи дату как ДД.ММ.ГГГГ, например: 31.03.2026")
        return

    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await message.answer("Задача не найдена.")
            await state.clear()
            return
        task.deadline = deadline
        task.is_verified = True
        await session.commit()
        task_title = task.title

    await state.clear()
    deadline_str = deadline.strftime('%d.%m.%Y')
    await message.answer(
        f"✅ <b>Задача верифицирована!</b>\n"
        f"📌 {escape(task_title)}\n"
        f"📅 {deadline_str}",
        parse_mode="HTML",
    )

    # Notify assignee (need bot instance from state data)
    async with async_session() as session:
        task_obj = await session.get(Task, task_id)
        assignee = await session.get(Member, task_obj.assignee_id) if task_obj and task_obj.assignee_id else None
    # Note: can't easily get bot here from message handler, will notify via scheduler later

    tasks = await _get_unverified_tasks()
    if tasks:
        await _show_task(message, tasks[0], len(tasks), 1)
    else:
        await message.answer("🎉 <b>Все задачи верифицированы!</b>", parse_mode="HTML")


# ── Delete task ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("vt_del:"))
async def cb_delete_task(callback: CallbackQuery):
    """Delete a task that doesn't belong in the registry."""
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return

    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        title = task.title if task else "?"
        await session.execute(sa_delete(Task).where(Task.id == task_id))
        await session.commit()

    await callback.answer("🗑 Удалено")
    await callback.message.answer(
        f"🗑 <b>Задача удалена</b>\n<i>{escape(title)}</i>",
        parse_mode="HTML",
    )
    await _show_next(callback)


# ── Notify assignee after verification ─────────────────────────────────────────

async def _notify_assignee(bot: Bot, task_id: int, task_title: str, assignee: Member | None, deadline_str: str):
    """Send task notification to assignee after chairman verifies it."""
    if not assignee or not assignee.telegram_id or assignee.telegram_id < 0:
        return
    try:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks")]
        ])
        await bot.send_message(
            assignee.telegram_id,
            f"🔔 <b>Новая задача назначена</b>\n\n"
            f"<b>#{task_id}</b> {escape(task_title)}\n"
            f"📅 Срок: {deadline_str}\n\n"
            f"<i>Задача добавлена в твой список.</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning(f"Could not notify assignee {assignee.telegram_id}: {e}")


# ── Show next unverified task ──────────────────────────────────────────────────

async def _show_next(callback: CallbackQuery):
    tasks = await _get_unverified_tasks()
    if not tasks:
        await callback.message.answer("🎉 <b>Все задачи верифицированы!</b>", parse_mode="HTML")
    else:
        await _show_task(callback, tasks[0], len(tasks), 1)
