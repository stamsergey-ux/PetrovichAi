"""Stakeholder/shareholder handler — task assignment with confirmation flow."""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select

from app.database import async_session, Task, Member
from app.ai_service import parse_stakeholder_task
from app.utils import is_stakeholder, is_chairman

logger = logging.getLogger(__name__)
router = Router()


class TaskCreation(StatesGroup):
    waiting_for_description = State()
    waiting_for_confirmation = State()


# ── Entry point: stakeholder presses the button ──────────────────────────────

@router.message(F.text.lower().in_({"💎 поставить задачу", "поставить задачу", "создать поручение"}))
async def start_task_creation(message: Message, state: FSMContext):
    if not is_stakeholder(message.from_user.username):
        return
    await state.set_state(TaskCreation.waiting_for_description)
    await message.answer(
        "💎 <b>Постановка задачи</b>\n\n"
        "Опишите задачу голосом или текстом. Укажите:\n"
        "  • <b>Что</b> нужно сделать\n"
        "  • <b>Кто</b> ответственный\n"
        "  • <b>Срок</b> выполнения\n\n"
        "<i>Пример: «Подготовить финансовый отчёт за Q1, ответственная Виктория, срок до 15 апреля»</i>",
        parse_mode="HTML",
    )


# ── Receive description (text) ────────────────────────────────────────────────

@router.message(TaskCreation.waiting_for_description, F.text)
async def receive_task_text(message: Message, state: FSMContext):
    await _parse_and_confirm(message, state, message.text)


# ── Receive description (voice) ───────────────────────────────────────────────

@router.message(TaskCreation.waiting_for_description, F.voice)
async def receive_task_voice(message: Message, state: FSMContext, bot: Bot):
    await message.answer("🎙 Распознаю голосовое сообщение...")
    try:
        from app.voice import transcribe_voice
        file = await bot.download(message.voice)
        text = await transcribe_voice(file.read(), ".ogg")
        if not text:
            await message.answer("⚠️ Не удалось распознать. Попробуй ещё раз или напиши текстом.")
            return
        await message.answer(f"📝 <i>Распознано:</i>\n{text}", parse_mode="HTML")
        await _parse_and_confirm(message, state, text)
    except Exception as e:
        logger.error(f"Stakeholder voice error: {e}")
        await message.answer(f"❌ Ошибка распознавания: {e}")


async def _parse_and_confirm(message: Message, state: FSMContext, text: str):
    """Parse task text with AI and show confirmation card."""
    await message.answer("🤖 Разбираю задачу...")

    async with async_session() as session:
        members = (await session.execute(
            select(Member).where(Member.is_active == True)
        )).scalars().all()

    members_list = ", ".join(m.name for m in members)
    parsed = await parse_stakeholder_task(text, members_list)

    # Store in FSM state
    await state.update_data(
        parsed=parsed,
        original_text=text,
        members_map={m.name: m.id for m in members},
        members_tg={m.id: m.telegram_id for m in members},
    )
    await state.set_state(TaskCreation.waiting_for_confirmation)

    # Build confirmation card
    assignee = escape(parsed.get("assignee_name") or "Не определён")
    deadline_raw = parsed.get("deadline")
    deadline_str = deadline_raw or "Не указан"
    title = escape(parsed.get("title") or text[:100])
    description = parsed.get("description") or ""
    priority_map = {"high": "🔴 Высокий", "medium": "🟡 Средний", "low": "🟢 Низкий"}
    priority = priority_map.get(parsed.get("priority", "high"), "🔴 Высокий")

    card = (
        f"💎 <b>ЗАДАЧА ОТ АКЦИОНЕРА</b>\n\n"
        f"📌 <b>Задача:</b> {title}\n"
        f"👤 <b>Ответственный:</b> {assignee}\n"
        f"📅 <b>Срок:</b> {deadline_str}\n"
        f"⚡ <b>Приоритет:</b> {priority}\n"
    )
    if description and description != title:
        card += f"\n📝 <b>Описание:</b>\n{escape(description)}\n"
    card += "\n<i>Всё правильно?</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="stk_confirm"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="stk_retry"),
    ]])
    await message.answer(card, parse_mode="HTML", reply_markup=keyboard)


# ── Confirm → create task ─────────────────────────────────────────────────────

@router.callback_query(F.data == "stk_confirm", TaskCreation.waiting_for_confirmation)
async def confirm_task(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    parsed = data["parsed"]
    members_map: dict[str, int] = data["members_map"]
    members_tg: dict[int, int] = data["members_tg"]

    # Match assignee
    assignee_id = None
    assignee_name = parsed.get("assignee_name") or ""
    for name, mid in members_map.items():
        if assignee_name.lower() in name.lower() or name.lower() in assignee_name.lower():
            assignee_id = mid
            break

    # Parse deadline
    deadline = None
    deadline_str = parsed.get("deadline")
    if deadline_str:
        try:
            deadline = datetime.fromisoformat(deadline_str)
        except Exception:
            pass

    # Get stakeholder's member record
    async with async_session() as session:
        creator = (await session.execute(
            select(Member).where(Member.telegram_id == callback.from_user.id)
        )).scalar_one_or_none()
        creator_id = creator.id if creator else None

        task = Task(
            title=parsed.get("title") or data["original_text"][:500],
            description=parsed.get("description"),
            assignee_id=assignee_id,
            deadline=deadline,
            priority=parsed.get("priority", "high"),
            status="new",
            source="stakeholder",
            created_by_id=creator_id,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    await state.clear()
    await callback.answer()

    await callback.message.answer(
        f"✅ <b>Задача #{task_id} поставлена!</b>\n"
        f"Ответственный и руководство будут уведомлены.",
        parse_mode="HTML",
    )

    # Notify assignee
    if assignee_id and assignee_id in members_tg:
        tg_id = members_tg[assignee_id]
        if tg_id and tg_id > 0:
            try:
                deadline_disp = deadline.strftime("%d.%m.%Y") if deadline else "не указан"
                await bot.send_message(
                    tg_id,
                    f"💎 <b>Новая задача от акционера</b>\n\n"
                    f"<b>#{task_id}</b> {escape(parsed.get('title', ''))}\n"
                    f"📅 Срок: {deadline_disp}\n\n"
                    f"<i>Уточни детали у секретаря или ответь на это сообщение боту.</i>",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Could not notify assignee {tg_id}: {e}")

    # Notify chairmen
    async with async_session() as session:
        chairmen = (await session.execute(
            select(Member).where(Member.is_chairman == True)
        )).scalars().all()
    for ch in chairmen:
        if ch.telegram_id > 0 and ch.telegram_id != callback.from_user.id:
            try:
                await bot.send_message(
                    ch.telegram_id,
                    f"💎 <b>Акционер поставил задачу #{task_id}</b>\n"
                    f"{escape(parsed.get('title', ''))}\n"
                    f"→ {escape(assignee_name or 'Без ответственного')} | {deadline_str or 'без срока'}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Could not notify chairman {ch.telegram_id}: {e}")


# ── Retry → go back to description ───────────────────────────────────────────

@router.callback_query(F.data == "stk_retry")
async def retry_task(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TaskCreation.waiting_for_description)
    await callback.answer()
    await callback.message.answer(
        "✏️ Опишите задачу заново — голосом или текстом:",
    )


# ── "Мои поручения" — stakeholder views their own assigned tasks ──────────────

@router.message(F.text.lower().in_({"💎 мои поручения", "мои поручения"}))
async def show_my_assignments(message: Message):
    if not is_stakeholder(message.from_user.username):
        return
    await _render_my_assignments(message)


@router.callback_query(F.data == "stk_my_tasks")
async def cb_my_assignments(callback: CallbackQuery):
    await callback.answer()
    await _render_my_assignments(callback.message, user_id=callback.from_user.id)


async def _render_my_assignments(message: Message, user_id: int | None = None):
    uid = user_id or message.from_user.id
    async with async_session() as session:
        creator = (await session.execute(
            select(Member).where(Member.telegram_id == uid)
        )).scalar_one_or_none()

        if not creator:
            await message.answer("Ты не зарегистрирован. Напиши /start")
            return

        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.created_by_id == creator.id)
            .order_by(Task.created_at.desc())
        )
        rows = result.all()

    if not rows:
        await message.answer("💎 У тебя пока нет поставленных задач.")
        return

    total = len(rows)
    done = sum(1 for t, _ in rows if t.status == "done")
    open_ = sum(1 for t, _ in rows if t.status in ("new", "in_progress"))
    overdue = sum(1 for t, _ in rows if t.status == "overdue")

    text = f"💎 <b>МОИ ПОРУЧЕНИЯ</b> — {total} задач\n"
    text += f"✅ Выполнено: {done} | 🔵 В работе: {open_} | 🔴 Просрочено: {overdue}\n\n"

    status_icons = {"new": "⬜", "in_progress": "🔵", "done": "✅", "overdue": "🔴"}
    for task, assignee in rows:
        icon = status_icons.get(task.status, "⬜")
        dl = task.deadline.strftime("%d.%m.%Y") if task.deadline else "—"
        assignee_name = assignee.name if assignee else "Не назначено"
        title = escape(task.title[:60] + ("..." if len(task.title) > 60 else ""))
        text += f"{icon} <b>#{task.id}</b> {title}\n"
        text += f"   👤 {escape(assignee_name)} · 📅 {dl}\n\n"

    if len(text) > 4000:
        text = text[:4000] + "\n... <i>обрезано</i>"

    await message.answer(text, parse_mode="HTML")


# ── Admin: view all stakeholder tasks ────────────────────────────────────────

@router.callback_query(F.data == "stk_all_tasks")
async def cb_stakeholder_all_tasks(callback: CallbackQuery):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return
    await callback.answer()
    await _render_stakeholder_tasks(callback.message)


@router.message(F.text.lower().in_({"задачи акционера", "💎 задачи акционера"}))
async def show_stakeholder_tasks(message: Message):
    if not is_chairman(message.from_user.username):
        return
    await _render_stakeholder_tasks(message)


async def _render_stakeholder_tasks(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .where(Task.source == "stakeholder")
            .order_by(Task.created_at.desc())
        )
        rows = result.all()

    if not rows:
        await message.answer("💎 Задач от акционера пока нет.")
        return

    total = len(rows)
    done = sum(1 for t, _ in rows if t.status == "done")
    overdue = sum(1 for t, _ in rows if t.status == "overdue")

    text = f"💎 <b>ЗАДАЧИ АКЦИОНЕРА</b> — {total} шт.\n"
    text += f"✅ Выполнено: {done} | 🔴 Просрочено: {overdue}\n\n"

    status_icons = {"new": "⬜", "in_progress": "🔵", "done": "✅", "overdue": "🔴"}
    for task, assignee in rows:
        icon = status_icons.get(task.status, "⬜")
        dl = task.deadline.strftime("%d.%m.%Y") if task.deadline else "—"
        assignee_name = assignee.name if assignee else "Не назначено"
        title = escape(task.title[:60] + ("..." if len(task.title) > 60 else ""))
        text += f"{icon} <b>#{task.id}</b> {title}\n"
        text += f"   👤 {escape(assignee_name)} · 📅 {dl}\n\n"

    if len(text) > 4000:
        text = text[:4000] + "\n... <i>обрезано</i>"

    await message.answer(text, parse_mode="HTML")
