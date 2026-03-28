"""Personal tasks / reminders — quick capture via text or voice."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from html import escape

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select

from app.database import async_session, PersonalTask, Member

logger = logging.getLogger(__name__)
router = Router()


# ── FSM for capturing a personal task ────────────────────────────────────────

class PersonalTaskFSM(StatesGroup):
    waiting_description = State()


# ── Entry points ─────────────────────────────────────────────────────────────

PERSONAL_TASKS_USERS = {"vikamikhno"}  # Pilot: only Виктория Михно


def _has_personal_access(username: str | None) -> bool:
    return bool(username and username.lower() in PERSONAL_TASKS_USERS)


@router.message(F.text.lower().in_({
    "📝 записать задачу", "записать задачу", "напоминалка",
    "напомни", "запиши", "заметка",
}))
async def start_personal_task(message: Message, state: FSMContext):
    if not _has_personal_access(message.from_user.username):
        return  # silently ignore for non-pilot users
    await state.set_state(PersonalTaskFSM.waiting_description)
    await message.answer(
        "📝 <b>Личная задача</b>\n\n"
        "Опиши голосом или текстом, что нужно сделать.\n"
        "Можешь указать срок:\n"
        "  <i>«Позвонить в банк завтра»</i>\n"
        "  <i>«Подготовить отчёт до пятницы»</i>\n"
        "  <i>«Купить подарок через 3 дня»</i>",
        parse_mode="HTML",
    )


@router.message(PersonalTaskFSM.waiting_description, F.text)
async def receive_personal_text(message: Message, state: FSMContext):
    await _save_personal_task(message, state, message.text)


@router.message(PersonalTaskFSM.waiting_description, F.voice)
async def receive_personal_voice(message: Message, state: FSMContext, bot: Bot):
    await message.answer("🎙 Распознаю...")
    try:
        from app.voice import transcribe_voice
        file = await bot.download(message.voice)
        text = await transcribe_voice(file.read(), ".ogg")
        if not text:
            await message.answer("⚠️ Не удалось распознать. Попробуй текстом.")
            return
        await message.answer(f"📝 <i>{escape(text)}</i>", parse_mode="HTML")
        await _save_personal_task(message, state, text)
    except Exception as e:
        logger.error(f"Personal voice error: {e}")
        await message.answer(f"❌ Ошибка: {e}")


async def _save_personal_task(message: Message, state: FSMContext, text: str):
    """Parse text, extract date if present, and save."""
    await state.clear()

    title, remind_at = _parse_reminder(text)

    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == message.from_user.id)
        )).scalar_one_or_none()

        if not member:
            await message.answer("Нажми /start для регистрации.")
            return

        task = PersonalTask(
            owner_id=member.id,
            title=title,
            remind_at=remind_at,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    remind_str = remind_at.strftime("%d.%m.%Y %H:%M") if remind_at else "без напоминания"
    await message.answer(
        f"✅ <b>Записано!</b>\n\n"
        f"📝 {escape(title)}\n"
        f"🔔 {remind_str}\n\n"
        f"<i>Посмотреть все: «Мои заметки»</i>",
        parse_mode="HTML",
    )


# ── List personal tasks ──────────────────────────────────────────────────────

@router.message(F.text.lower().in_({"📋 мои заметки", "мои заметки", "мои напоминалки", "заметки"}))
async def show_personal_tasks(message: Message):
    if not _has_personal_access(message.from_user.username):
        return
    await _render_personal_tasks(message)


@router.callback_query(F.data == "personal_tasks")
async def cb_personal_tasks(callback: CallbackQuery):
    await callback.answer()
    await _render_personal_tasks(callback.message, user_id=callback.from_user.id)


async def _render_personal_tasks(message: Message, user_id: int | None = None):
    uid = user_id or message.from_user.id
    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == uid)
        )).scalar_one_or_none()
        if not member:
            await message.answer("Нажми /start для регистрации.")
            return

        result = await session.execute(
            select(PersonalTask)
            .where(PersonalTask.owner_id == member.id, PersonalTask.is_done == False)
            .order_by(PersonalTask.remind_at.asc().nulls_last(), PersonalTask.created_at.desc())
        )
        tasks = result.scalars().all()

    if not tasks:
        await message.answer(
            "📝 <b>Нет активных заметок</b>\n\n<i>Напиши «Записать задачу» чтобы добавить.</i>",
            parse_mode="HTML",
        )
        return

    text = f"📝 <b>Мои заметки</b> — {len(tasks)}\n\n"
    buttons = []
    for t in tasks:
        remind = t.remind_at.strftime("%d.%m %H:%M") if t.remind_at else ""
        icon = "🔔" if t.remind_at and not t.reminded else "📝"
        text += f"{icon} {escape(t.title[:60])}"
        if remind:
            text += f"  <i>({remind})</i>"
        text += "\n"
        buttons.append([
            InlineKeyboardButton(
                text=f"✅ {t.title[:40]}",
                callback_data=f"ptask_done:{t.id}",
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("ptask_done:"))
async def cb_personal_done(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        task = await session.get(PersonalTask, task_id)
        if task:
            await session.delete(task)
            await session.commit()
    await callback.answer("✅ Удалено!")
    await callback.message.answer("✅ Задача выполнена и удалена из списка.")


# ── Date parsing helpers ─────────────────────────────────────────────────────

_DAY_NAMES_RU = {
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2,
    "четверг": 3, "пятница": 4, "пятницу": 4,
    "суббота": 5, "субботу": 5, "воскресенье": 6,
}


def _parse_reminder(text: str) -> tuple[str, datetime | None]:
    """Extract a reminder date from natural Russian text. Returns (clean_title, remind_at)."""
    now = datetime.utcnow()
    lower = text.lower()

    # "завтра"
    if "завтра" in lower:
        remind_at = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        title = re.sub(r'\bзавтра\b', '', text, flags=re.IGNORECASE).strip()
        return title or text, remind_at

    # "послезавтра"
    if "послезавтра" in lower:
        remind_at = (now + timedelta(days=2)).replace(hour=6, minute=0, second=0, microsecond=0)
        title = re.sub(r'\bпослезавтра\b', '', text, flags=re.IGNORECASE).strip()
        return title or text, remind_at

    # "через N дней/дня"
    m = re.search(r'через\s+(\d+)\s+(?:дн|день|дня|дней)', lower)
    if m:
        days = int(m.group(1))
        remind_at = (now + timedelta(days=days)).replace(hour=6, minute=0, second=0, microsecond=0)
        title = text[:m.start()] + text[m.end():]
        return title.strip() or text, remind_at

    # "до пятницы", "в среду", etc.
    for day_name, weekday in _DAY_NAMES_RU.items():
        if day_name in lower:
            days_ahead = (weekday - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            remind_at = (now + timedelta(days=days_ahead)).replace(
                hour=6, minute=0, second=0, microsecond=0
            )
            title = re.sub(
                rf'(?:до|в|к)\s+{re.escape(day_name)}', '', text, flags=re.IGNORECASE
            ).strip()
            return title or text, remind_at

    # "до DD.MM" or "до DD.MM.YYYY"
    m = re.search(r'до\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?', text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            remind_at = datetime(year, month, day, 6, 0, 0)
            if remind_at < now:
                remind_at = remind_at.replace(year=year + 1)
            title = text[:m.start()] + text[m.end():]
            return title.strip() or text, remind_at
        except ValueError:
            pass

    return text, None


# ── Quick save without FSM (from chat dispatch) ──────────────────────────────

async def _save_personal_task_direct(message: Message, text: str):
    """Save a personal task directly from chat dispatch (e.g. 'напомни позвонить завтра')."""
    # Strip command prefix
    for prefix in ("напомни ", "запиши ", "заметка "):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break

    title, remind_at = _parse_reminder(text)

    async with async_session() as session:
        member = (await session.execute(
            select(Member).where(Member.telegram_id == message.from_user.id)
        )).scalar_one_or_none()
        if not member:
            await message.answer("Нажми /start для регистрации.")
            return

        task = PersonalTask(
            owner_id=member.id,
            title=title,
            remind_at=remind_at,
        )
        session.add(task)
        await session.commit()

    remind_str = remind_at.strftime("%d.%m.%Y %H:%M") if remind_at else "без напоминания"
    await message.answer(
        f"✅ <b>Записано!</b>\n\n"
        f"📝 {escape(title)}\n"
        f"🔔 {remind_str}",
        parse_mode="HTML",
    )


# ── Scheduler: send reminders ─────────────────────────────────────────────────

async def check_personal_reminders(bot: Bot):
    """Send reminders for personal tasks that are due. Called from scheduler."""
    now = datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(PersonalTask, Member)
            .join(Member, PersonalTask.owner_id == Member.id)
            .where(
                PersonalTask.is_done == False,
                PersonalTask.reminded == False,
                PersonalTask.remind_at != None,
                PersonalTask.remind_at <= now,
            )
        )
        rows = result.all()

        for task, member in rows:
            if member.telegram_id and member.telegram_id > 0:
                try:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"ptask_done:{task.id}"),
                    ]])
                    await bot.send_message(
                        member.telegram_id,
                        f"🔔 <b>Напоминание</b>\n\n📝 {escape(task.title)}",
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception as e:
                    logger.warning(f"Personal reminder failed for {member.telegram_id}: {e}")
            task.reminded = True

        if rows:
            await session.commit()
