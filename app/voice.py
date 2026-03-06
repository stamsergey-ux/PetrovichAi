"""Voice message transcription using OpenAI Whisper API."""
from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger(__name__)


async def transcribe_voice(file_bytes: bytes, file_extension: str = ".ogg") -> str | None:
    """Transcribe voice message to text using OpenAI Whisper API."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as f:
            f.write(file_bytes)
            tmp_path = f.name

        try:
            with open(tmp_path, "rb") as audio_file:
                response = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="ru",
                )
            text = response.text.strip()
            logger.info(f"Transcribed {len(file_bytes)} bytes -> {len(text)} chars")
            return text if text else None
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Whisper API transcription error: {e}")
        return None
