"""Voice message transcription using local Whisper model (free, offline)."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Path to local ffmpeg binary (bundled with the project)
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FFMPEG_PATH = os.path.join(_PROJECT_DIR, "ffmpeg")

# Lazy-load whisper model
_model = None


def _get_model():
    global _model
    if _model is None:
        import whisper
        logger.info("Loading Whisper model (base)...")
        _model = whisper.load_model("base")
        logger.info("Whisper model loaded.")
    return _model


async def transcribe_voice(file_bytes: bytes, file_extension: str = ".ogg") -> str | None:
    """Transcribe voice message to text using local Whisper."""
    try:
        with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as f:
            f.write(file_bytes)
            tmp_path = f.name

        try:
            # Set ffmpeg path for whisper
            os.environ["PATH"] = _PROJECT_DIR + ":" + os.environ.get("PATH", "")

            model = _get_model()
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: model.transcribe(tmp_path, language="ru")
            )
            text = result.get("text", "").strip()
            logger.info(f"Transcribed {len(file_bytes)} bytes -> {len(text)} chars")
            return text if text else None
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Whisper transcription error: {e}")
        return None
