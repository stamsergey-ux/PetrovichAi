"""Meeting materials — upload and retrieve PDF/PPTX presentations."""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.database import async_session, MeetingMaterial, Member, Meeting

logger = logging.getLogger(__name__)
router = Router()

SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls"}


def _is_material_file(filename: str | None) -> bool:
    if not filename:
        return False
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in SUPPORTED_EXTENSIONS


def _file_type(filename: str | None) -> str:
    if not filename:
        return "other"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext or "other"


async def save_material(message: Message, description: str | None = None) -> bool:
    """Save uploaded document as a meeting material. Returns True on success."""
    doc = message.document
    if not doc:
        return False

    filename = doc.file_name or "файл"

    async with async_session() as session:
        # Find uploader's member record
        member = (await session.execute(
            select(Member).where(Member.telegram_id == message.from_user.id)
        )).scalar_one_or_none()

        # Link to most recent confirmed meeting if exists
        last_meeting = (await session.execute(
            select(Meeting)
            .where(Meeting.is_confirmed == True)
            .order_by(Meeting.date.desc())
            .limit(1)
        )).scalar_one_or_none()

        material = MeetingMaterial(
            uploader_id=member.id if member else None,
            meeting_id=last_meeting.id if last_meeting else None,
            file_id=doc.file_id,
            file_name=filename,
            file_type=_file_type(filename),
            description=description or message.caption,
        )
        session.add(material)
        await session.commit()
        material_id = material.id

    uploader_name = message.from_user.first_name or message.from_user.username or "Участник"
    meeting_ref = f" к совещанию «{last_meeting.title}»" if last_meeting else ""

    await message.answer(
        f"📎 <b>Материал сохранён!</b>{meeting_ref}\n\n"
        f"📄 {escape(filename)}\n"
        f"👤 Загрузил: {escape(uploader_name)}\n"
        f"🆔 #{material_id}\n\n"
        f"<i>Доступен в разделе «📎 Материалы»</i>",
        parse_mode="HTML",
    )
    return True


async def show_materials(message: Message):
    """Show list of stored materials with buttons to retrieve them."""
    async with async_session() as session:
        result = await session.execute(
            select(MeetingMaterial, Member)
            .outerjoin(Member, MeetingMaterial.uploader_id == Member.id)
            .order_by(MeetingMaterial.created_at.desc())
            .limit(30)
        )
        rows = result.all()

    if not rows:
        await message.answer(
            "📭 <b>Материалы пока не загружены.</b>\n\n"
            "<i>Участники могут загружать PDF, PPTX и другие файлы прямо в чат с ботом.</i>",
            parse_mode="HTML",
        )
        return

    text = f"📎 <b>МАТЕРИАЛЫ СОВЕЩАНИЙ</b> — {len(rows)} файлов\n\n"

    buttons = []
    for mat, member in rows:
        date_str = mat.created_at.strftime("%d.%m.%Y")
        uploader = member.display_name or member.first_name or member.username if member else "?"
        icon = {"pdf": "📕", "pptx": "📊", "ppt": "📊", "docx": "📝", "xlsx": "📈"}.get(mat.file_type, "📄")
        name = (mat.file_name or "файл")[:45]
        desc = f" — {mat.description[:30]}" if mat.description else ""
        text += f"{icon} <b>{escape(name)}</b>{escape(desc)}\n"
        text += f"   👤 {escape(uploader)} · 📅 {date_str}\n\n"

        buttons.append([InlineKeyboardButton(
            text=f"{icon} {name[:35]}",
            callback_data=f"mat_get:{mat.id}",
        )])

    if len(text) > 3800:
        text = text[:3800] + "\n\n... <i>список обрезан</i>"

    text += "\n<i>Нажми на файл, чтобы получить его:</i>"
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("mat_get:"))
async def cb_get_material(callback: CallbackQuery, bot: Bot):
    """Send the requested material file to the user."""
    mat_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        mat = await session.get(MeetingMaterial, mat_id)

    if not mat:
        await callback.answer("Файл не найден.", show_alert=True)
        return

    await callback.answer()
    try:
        caption = mat.description or mat.file_name or "Материал совещания"
        await bot.send_document(
            callback.from_user.id,
            mat.file_id,
            caption=f"📎 {escape(caption)}",
        )
    except Exception as e:
        logger.error(f"Failed to send material {mat_id}: {e}")
        await callback.message.answer(f"❌ Не удалось отправить файл: {e}")
