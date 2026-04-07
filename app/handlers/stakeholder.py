"""Stakeholder handler — task creation removed. View-only access to protocols and tasks."""
from __future__ import annotations

from html import escape

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.database import async_session, Task, Member
from app.utils import is_stakeholder, is_chairman

router = Router()


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
            await message.answer("Нажми /start для регистрации.")
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
    overdue = sum(1 for t, _ in rows if t.status == "overdue")
    badges = [f"🚨 {overdue} просрочено"] if overdue else []
    badge_str = ("\n" + " · ".join(badges)) if badges else ""
    text = f"💎 <b>МОИ ПОРУЧЕНИЯ</b> — {total} задач{badge_str}"

    from app.handlers.tasks import _task_buttons
    task_rows = _task_buttons(rows, show_assignee=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=task_rows) if task_rows else None
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "stk_all_tasks")
async def cb_stakeholder_all_tasks(callback: CallbackQuery):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для администраторов", show_alert=True)
        return
    await callback.answer()
    await _render_stakeholder_tasks(callback.message)


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
    overdue = sum(1 for t, _ in rows if t.status == "overdue")
    badge_str = f"\n🚨 {overdue} просрочено" if overdue else ""
    text = f"💎 <b>ЗАДАЧИ АКЦИОНЕРА</b> — {total} шт.{badge_str}"

    from app.handlers.tasks import _task_buttons
    task_rows = _task_buttons(rows, show_assignee=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=task_rows) if task_rows else None
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
