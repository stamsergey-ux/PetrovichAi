"""Chairman task assignment — voice/text task creation with assignee acknowledgment."""
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
from app.utils import is_chairman

logger = logging.getLogger(__name__)
router = Router()


class ChairmanTaskState(StatesGroup):
    waiting_for_description = State()
    waiting_for_confirmation = State()


# ── Entry point ────────────────────────────────────────────────────────────────

@router.message(F.text.lower().in_({"📝 поставить задачу", "поставить задачу"}))
async def start_chairman_task(message: Message, state: FSMContext):
    if not is_chairman(message.from_user.username):
        return
    await state.set_state(ChairmanTaskState.waiting_for_description)
    await message.answer(
        "📝 <b>Постановка задачи</b>\n\n"
        "Опиши задачу голосом или текстом. Укажи:\n"
        "  · <b>Что</b> нужно сделать\n"
        "  · <b>Кому</b> (имя члена СД)\n"
        "  · <b>Срок</b> выполнения\n\n"
        "<i>Пример: «Екатерина, подготовь отчёт по аудиту до 20 марта, высокий приоритет»</i>",
        parse_mode="HTML",
    )


# ── Receive description: text ──────────────────────────────────────────────────

@router.message(ChairmanTaskState.waiting_for_description, F.text)
async def receive_task_text(message: Message, state: FSMContext):
    await _parse_and_confirm(message, state, message.text)


# ── Receive description: voice ─────────────────────────────────────────────────

@router.message(ChairmanTaskState.waiting_for_description, F.voice)
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
        logger.error(f"Chairman task voice error: {e}")
        await message.answer(f"❌ Ошибка распознавания: {e}")


# ── Parse with AI and show confirmation card ───────────────────────────────────

async def _parse_and_confirm(message: Message, state: FSMContext, text: str):
    await message.answer("🤖 Разбираю задачу...")

    async with async_session() as session:
        members = (await session.execute(
            select(Member).where(Member.is_active == True)
        )).scalars().all()

    members_list = ", ".join(m.name for m in members)
    parsed = await parse_stakeholder_task(text, members_list)

    await state.update_data(
        parsed=parsed,
        original_text=text,
        members_map={m.name: m.id for m in members},
        members_tg={m.id: m.telegram_id for m in members},
    )
    await state.set_state(ChairmanTaskState.waiting_for_confirmation)

    assignee = escape(parsed.get("assignee_name") or "Не определён")
    deadline_raw = parsed.get("deadline")
    deadline_str = deadline_raw or "Не указан"
    title = escape(parsed.get("title") or text[:100])
    description = parsed.get("description") or ""
    priority_map = {"high": "🔴 Высокий", "medium": "🟡 Средний", "low": "🟢 Низкий"}
    priority = priority_map.get(parsed.get("priority", "medium"), "🟡 Средний")

    card = (
        f"📝 <b>ЗАДАЧА ОТ ПРЕДСЕДАТЕЛЯ</b>\n\n"
        f"📌 <b>Задача:</b> {title}\n"
        f"👤 <b>Исполнитель:</b> {assignee}\n"
        f"📅 <b>Срок:</b> {deadline_str}\n"
        f"⚡ <b>Приоритет:</b> {priority}\n"
    )
    if description and description != parsed.get("title"):
        card += f"\n📝 <b>Описание:</b>\n{escape(description)}\n"
    card += "\n<i>Всё правильно?</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Поставить задачу", callback_data="cht_confirm"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="cht_retry"),
    ]])
    await message.answer(card, parse_mode="HTML", reply_markup=keyboard)


# ── Confirm → create task ──────────────────────────────────────────────────────

@router.callback_query(F.data == "cht_confirm", ChairmanTaskState.waiting_for_confirmation)
async def confirm_chairman_task(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_chairman(callback.from_user.username):
        await callback.answer("⛔ Только для председателя", show_alert=True)
        return

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

    # Get chairman's member record
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
            priority=parsed.get("priority", "medium"),
            status="new",
            source="manual",
            created_by_id=creator_id,
            is_verified=True,  # chairman's direct assignment — no verification needed
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    await state.clear()
    await callback.answer()

    deadline_disp = deadline.strftime("%d.%m.%Y") if deadline else "без срока"
    creator_name = creator.display_name or creator.first_name or "Председатель" if creator else "Председатель"

    await callback.message.answer(
        f"✅ <b>Задача #{task_id} поставлена!</b>\n"
        f"Ожидаю подтверждения от исполнителя.",
        parse_mode="HTML",
    )

    # Notify assignee with acknowledgment button
    if assignee_id and assignee_id in members_tg:
        tg_id = members_tg[assignee_id]
        if tg_id and tg_id > 0:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Принял задачу",
                    callback_data=f"task_ack:{task_id}:{callback.from_user.id}",
                )
            ]])
            try:
                await bot.send_message(
                    tg_id,
                    f"📝 <b>Новая задача от {escape(creator_name)}</b>\n\n"
                    f"<b>#{task_id}</b> {escape(parsed.get('title', ''))}\n"
                    f"📅 Срок: {deadline_disp}\n"
                    f"⚡ Приоритет: {parsed.get('priority', 'medium')}\n\n"
                    f"<i>Нажми кнопку, чтобы подтвердить получение задачи.</i>",
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.warning(f"Could not notify assignee {tg_id}: {e}")


# ── Assignee acknowledges receipt ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("task_ack:"))
async def task_acknowledgment(callback: CallbackQuery, bot: Bot):
    """Assignee confirms they received the task — notify chairman."""
    parts = callback.data.split(":")
    task_id = int(parts[1])
    chairman_tg_id = int(parts[2])

    async with async_session() as session:
        task = await session.get(Task, task_id)
        assignee = (await session.execute(
            select(Member).where(Member.telegram_id == callback.from_user.id)
        )).scalar_one_or_none()

    if not task:
        await callback.answer("Задача не найдена.", show_alert=True)
        return

    assignee_name = assignee.display_name or assignee.first_name or callback.from_user.first_name if assignee else callback.from_user.first_name
    deadline_str = task.deadline.strftime("%d.%m.%Y") if task.deadline else "без срока"

    await callback.answer("✅ Подтверждено!")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✅ <b>Задача #{task_id} принята!</b>\n"
        f"📅 Срок: {deadline_str}\n\n"
        f"<i>Задача добавлена в твой список. Удачи!</i>",
        parse_mode="HTML",
    )

    # Notify chairman
    try:
        await bot.send_message(
            chairman_tg_id,
            f"✅ <b>Задача #{task_id} принята исполнителем</b>\n\n"
            f"📌 {escape(task.title)}\n"
            f"👤 {escape(assignee_name)} подтвердил получение\n"
            f"📅 Срок: {deadline_str}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not notify chairman {chairman_tg_id}: {e}")


# ── Retry ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "cht_retry")
async def retry_chairman_task(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ChairmanTaskState.waiting_for_description)
    await callback.answer()
    await callback.message.answer("✏️ Опиши задачу заново — голосом или текстом:")
