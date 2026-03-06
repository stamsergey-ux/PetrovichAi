"""Voice message handler — transcribe and process as text."""
from __future__ import annotations

import logging

from aiogram import Router, F, Bot
from aiogram.types import Message

from app.voice import transcribe_voice

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.voice)
async def handle_voice(message: Message, bot: Bot):
    """Handle voice messages: transcribe via Whisper, then process as text command."""
    await message.answer("🎙 Распознаю голосовое сообщение...")

    try:
        # Download voice file
        file = await bot.download(message.voice)
        file_bytes = file.read()

        # Transcribe
        text = await transcribe_voice(file_bytes, ".ogg")

        if not text:
            await message.answer("⚠️ Не удалось распознать голосовое сообщение. Попробуй ещё раз или напиши текстом.")
            return

        # Show transcription
        await message.answer(f"📝 <i>Распознано:</i>\n{text}", parse_mode="HTML")

        # Now process the transcribed text as if user typed it
        # Create a fake-ish approach: just set message.text and call the text handler
        # Instead, import and call directly
        from app.handlers.chat import handle_text_message

        # We need to temporarily set text on the message for the handler
        message.text = text
        await handle_text_message(message)

    except Exception as e:
        logger.error(f"Voice processing error: {e}")
        await message.answer(f"❌ Ошибка обработки голосового: {e}")


@router.message(F.video_note)
async def handle_video_note(message: Message, bot: Bot):
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

        from app.handlers.chat import handle_text_message
        message.text = text
        await handle_text_message(message)

    except Exception as e:
        logger.error(f"Video note processing error: {e}")
        await message.answer(f"❌ Ошибка обработки видеосообщения: {e}")
