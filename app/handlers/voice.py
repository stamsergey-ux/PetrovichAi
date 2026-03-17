"""Voice message handler — transcribe and process as text."""
from __future__ import annotations

import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.voice import transcribe_voice

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.voice)
async def handle_voice(message: Message, bot: Bot, state: FSMContext):
    """Handle voice messages: transcribe via Whisper, then process as text command."""
    await message.answer("🎙 Распознаю голосовое сообщение...")

    try:
        file = await bot.download(message.voice)
        file_bytes = file.read()

        text = await transcribe_voice(file_bytes, ".ogg")

        if not text:
            await message.answer("⚠️ Не удалось распознать голосовое сообщение. Попробуй ещё раз или напиши текстом.")
            return

        await message.answer(f"📝 <i>Распознано:</i>\n{text}", parse_mode="HTML")

        from app.handlers.chat import _dispatch_text
        await _dispatch_text(message, text, state)

    except Exception as e:
        logger.error(f"Voice processing error: {e}")
        await message.answer(f"❌ Ошибка обработки голосового: {e}")


@router.message(F.video_note)
async def handle_video_note(message: Message, bot: Bot, state: FSMContext):
    """Handle video notes (round videos): transcribe audio track."""
    await message.answer("🎙 Распознаю аудио из видеосообщения...")

    try:
        file = await bot.download(message.video_note)
        file_bytes = file.read()

        text = await transcribe_voice(file_bytes, ".mp4")

        if not text:
            await message.answer("⚠️ Не удалось распознать речь из видеосообщения.")
            return

        await message.answer(f"📝 <i>Распознано:</i>\n{text}", parse_mode="HTML")

        from app.handlers.chat import _dispatch_text
        await _dispatch_text(message, text, state)

    except Exception as e:
        logger.error(f"Video note processing error: {e}")
        await message.answer(f"❌ Ошибка обработки видеосообщения: {e}")
