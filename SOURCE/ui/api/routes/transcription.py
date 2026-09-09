from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from uuid import uuid4

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from core.platform import get_temp_dir
from fastapi import Depends, File, HTTPException, Request, UploadFile
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth

log = get_logger(__name__)

# The multipart form field the transcribe routes read the audio from.
#
# FastAPI derives the field name from the handler's PARAMETER name, so this
# constant and the ``audio:`` parameter below must stay identical — and every
# in-process caller that POSTs to these routes imports this rather than
# spelling the name again. The voice-stream handler used to spell it "file",
# which FastAPI rejected with a 422 before reading a byte, breaking 100% of
# multiroom spoke voice turns while telling users "Could not understand
# speech". A mismatch is invisible at import time, so it is pinned by test
# (tests/unit/ui/api/routes/test_voice_stream_transcribe_seam.py).
TRANSCRIBE_FILE_FIELD = "audio"

# Allowed audio MIME types
_ALLOWED_AUDIO_MIME_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/webm",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
    "audio/flac",
    "audio/x-flac",
    "audio/aac",
    "audio/opus",
    "application/ogg",
    "application/octet-stream",  # Browser fallback
}

# Allowed file extensions
_ALLOWED_AUDIO_EXTENSIONS = {
    "wav",
    "webm",
    "ogg",
    "mp3",
    "mp4",
    "m4a",
    "flac",
    "aac",
    "opus",
    "wma",
    "oga",
}

# Magic byte signatures for audio formats (first few bytes)
_AUDIO_MAGIC_BYTES = {
    b"RIFF": "wav",  # WAV files start with RIFF
    b"ID3": "mp3",  # MP3 with ID3 tag
    b"\xff\xfb": "mp3",  # MP3 frame sync
    b"\xff\xfa": "mp3",  # MP3 frame sync
    b"\xff\xf3": "mp3",  # MP3 frame sync
    b"\xff\xf2": "mp3",  # MP3 frame sync
    b"OggS": "ogg",  # OGG/Opus container
    b"fLaC": "flac",  # FLAC
    b"\x1a\x45\xdf\xa3": "webm",  # WebM/Matroska
}

_STT_SETTING_DEFAULTS: dict[str, object] = {
    "whisper_model": "base",
    "whisper_device": "cpu",
    "whisper_language": None,
}


def _request_user_id(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()

    user_context = getattr(request.state, "user_context", None)
    context_user_id = getattr(user_context, "user_id", None)
    if isinstance(context_user_id, str) and context_user_id.strip():
        return context_user_id.strip()

    session = getattr(request.state, "session", None)
    session_user_id = getattr(session, "user_id", None)
    if isinstance(session_user_id, str) and session_user_id.strip():
        return session_user_id.strip()

    return None


def _settings_get(settings_mgr: Any, key: str, default: object, *, user_id: str | None) -> object:
    try:
        return settings_mgr.get(key, default, user_id=user_id)
    except TypeError:
        return settings_mgr.get(key, default)


def _stt_settings_snapshot(settings_mgr: Any, *, user_id: str | None) -> dict[str, object]:
    return {
        key: _settings_get(settings_mgr, key, default, user_id=user_id)
        for key, default in _STT_SETTING_DEFAULTS.items()
    }


def _validate_audio_file(content: bytes, filename: str | None, content_type: str | None) -> str | None:
    """Validate that uploaded content is a valid audio file.

    Returns the detected format or None if invalid.
    """
    # Check file extension
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext and ext not in _ALLOWED_AUDIO_EXTENSIONS:
            log.warning("Rejected upload: invalid extension %s", ext)
            return None

    # Check MIME type (if provided and not generic)
    if content_type and content_type != "application/octet-stream":
        mime_lower = content_type.lower().split(";")[0].strip()
        if mime_lower not in _ALLOWED_AUDIO_MIME_TYPES:
            log.warning("Rejected upload: invalid MIME type %s", content_type)
            return None

    # Check magic bytes
    if len(content) < 12:
        log.warning("Rejected upload: file too small to validate")
        return None

    header = content[:12]

    # Check standard magic bytes
    for magic, fmt in _AUDIO_MAGIC_BYTES.items():
        if header.startswith(magic):
            return fmt

    # Check for MP4/M4A (ftyp at offset 4)
    if len(content) >= 8 and content[4:8] == b"ftyp":
        return "m4a"

    # Check for AAC ADTS
    if len(content) >= 2 and content[0] == 0xFF and (content[1] & 0xF0) == 0xF0:
        return "aac"

    log.warning("Rejected upload: unrecognized audio format (header: %s)", header[:8].hex())
    return None


def register_transcription_routes(context: ApiContext) -> None:
    """Register transcription and audio ducking routes."""
    router = context.router
    resource_limits = context.resource_limits

    transcriber_cache: dict[str, Any] = {}

    async def _handle_transcription(request: Request, audio: UploadFile):
        """Shared transcription logic."""
        tmp_path: str | None = None
        try:
            content_length = audio.headers.get("content-length")
            if content_length:
                size = int(content_length)
                if size > resource_limits.max_file_size:
                    raise HTTPException(
                        status_code=413,
                        detail=(f"File too large. Maximum size: {resource_limits.max_file_size // (1024 * 1024)}MB"),
                    )

            resource_limits.validate_file_size(audio)

            # Read content with size limit
            max_size = resource_limits.max_file_size
            content = b""
            chunk_size = 1024 * 1024

            while True:
                chunk = await audio.read(chunk_size)
                if not chunk:
                    break
                content += chunk
                if len(content) > max_size:
                    raise HTTPException(
                        status_code=413,
                        detail=(f"File too large. Maximum size: {max_size // (1024 * 1024)}MB"),
                    )

            # Validate file type (MIME, extension, and magic bytes)
            detected_format = _validate_audio_file(content, audio.filename, audio.content_type)
            if detected_format is None:
                raise HTTPException(
                    status_code=415,
                    detail="Invalid audio file format. Supported formats: WAV, MP3, OGG, WEBM, FLAC, AAC, M4A",
                )

            # Use detected format for temp file extension
            suffix = f".{detected_format}"

            temp_root = get_temp_dir() / "transcription"
            temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=str(temp_root)) as tmp_file:
                tmp_file.write(content)
                tmp_path = tmp_file.name

            log.info(
                "🎤 Transcribing audio file: %s (%d bytes, format: %s)",
                tmp_path,
                len(content),
                suffix,
            )

            from ui.settings_manager import get_settings_manager

            use_deterministic = os.environ.get("VIOLA_TEST_TRANSCRIBER") == "1"
            settings_mgr = get_settings_manager()
            user_id = _request_user_id(request)
            stt_settings = _stt_settings_snapshot(settings_mgr, user_id=user_id)

            if use_deterministic:
                from voice.transcription.deterministic import DeterministicTranscriber

                cache_key = "deterministic:%s:%s" % (user_id or "device", tuple(sorted(stt_settings.items())))
                if cache_key not in transcriber_cache:
                    transcriber_cache[cache_key] = DeterministicTranscriber(stt_settings)
                    log.info("✅ Deterministic transcriber initialized for testing")
                else:
                    log.debug("🎤 Using cached deterministic transcriber")
            else:
                from voice.transcription.whisper import WhisperTranscriber

                cache_key = "whisper:%s:%s" % (user_id or "device", tuple(sorted(stt_settings.items())))
                if cache_key not in transcriber_cache:
                    log.info("🎤 Initializing Whisper transcriber (first time - may take a moment)...")
                    from config.settings import get_settings

                    config = get_settings()
                    # Off the event loop: WhisperTranscriber.__init__ loads the
                    # model synchronously, and on a cold install that is seconds
                    # of CPU (or, with no baked model and no network, a stack of
                    # connection timeouts). Constructing it inline froze the whole
                    # local API — every other request on this process, including
                    # the UI's own health polling — for the entire load.
                    transcriber_cache[cache_key] = await asyncio.to_thread(WhisperTranscriber, config, user_id=user_id)
                    log.info("✅ Whisper transcriber initialized successfully")
                else:
                    log.debug("🎤 Using cached Whisper transcriber")

            transcriber = transcriber_cache[cache_key]

            from utils.audio_ducking import duck_context

            with duck_context():
                transcript = await asyncio.to_thread(transcriber.transcribe, tmp_path)

            if transcript:
                log.info("Transcription successful length=%d", len(transcript))
                return success_response({"text": transcript, "transcript": transcript})

            log.warning("⚠️ Transcription returned empty result")
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "no_speech_detected",
                    "No speech was detected in the audio.",
                    data={"text": ""},
                ),
            )

        except HTTPException:
            raise
        except ImportError as exc:
            log.error("❌ Failed to import transcriber: %s", exc)
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "transcriber_not_available",
                    "Transcription is temporarily unavailable.",
                    data={"text": ""},
                ),
            )
        except Exception as exc:
            log.exception("❌ Transcription failed")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "transcription_failed",
                    "Transcription failed. Please try again.",
                    data={"text": ""},
                ),
            )
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except FileNotFoundError:
                    pass
                except (PermissionError, OSError) as exc:
                    log.debug("Could not delete temp file: %s", exc)

    @router.post("/v1/audio/duck", tags=["api", "audio"], dependencies=[Depends(require_auth)])
    async def start_ducking():
        try:
            from utils.audio_ducking import get_global_ducker

            ducker = get_global_ducker()
            if ducker:
                ducker.duck()
                log.debug("🔉 Audio ducking started via API")
                return success_response({"message": "Audio ducking started"})
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "ducking_unavailable",
                    "Audio ducking is temporarily unavailable.",
                ),
            )
        except Exception:
            log.exception("Failed to start ducking")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "ducking_error",
                    "Try again in a moment",
                ),
            )

    @router.post("/v1/audio/unduck", tags=["api", "audio"], dependencies=[Depends(require_auth)])
    async def stop_ducking():
        try:
            from utils.audio_ducking import get_global_ducker

            ducker = get_global_ducker()
            if ducker:
                ducker.unduck()
                log.debug("🔊 Audio ducking stopped via API")
                return success_response({"message": "Audio ducking stopped"})
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "ducking_unavailable",
                    "Audio ducking is temporarily unavailable.",
                ),
            )
        except Exception:
            log.exception("Failed to stop ducking")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "ducking_error",
                    "Try again in a moment",
                ),
            )

    @router.post(
        "/v1/transcribe",
        tags=["api", "transcription"],
        dependencies=[Depends(require_auth)],
    )
    async def transcribe_v1(request: Request, audio: UploadFile = File(...)):
        return await _handle_transcription(request, audio)

    @router.post(
        "/api/v1/transcribe",
        tags=["api", "transcription"],
        dependencies=[Depends(require_auth)],
    )
    async def transcribe_api_v1(request: Request, audio: UploadFile = File(...)):
        return await _handle_transcription(request, audio)

    log.info("✅ Transcription endpoints registered at /v1/transcribe and /api/v1/transcribe")

    @router.get("/api/v1/transcribe/test", dependencies=[Depends(require_auth)])
    async def test_transcriber(request: Request):
        try:
            from ui.settings_manager import get_settings_manager
            from voice.transcription.whisper import WhisperTranscriber

            settings_mgr = get_settings_manager()
            user_id = _request_user_id(request)
            stt_settings = _stt_settings_snapshot(settings_mgr, user_id=user_id)

            # Test transcriber initialization
            log.info("Testing transcriber initialization...")
            from config.settings import get_settings

            _ = WhisperTranscriber(get_settings(), user_id=user_id)  # Validate can be instantiated
            return success_response(
                {
                    "model": stt_settings["whisper_model"],
                    "device": stt_settings["whisper_device"],
                    "message": "Transcriber initialized successfully",
                }
            )
        except Exception:
            # Generate error ID for correlation (logged server-side, returned to client)
            error_id = str(uuid4())[:8]
            log.exception("Transcriber test failed (error_id=%s)", error_id)
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "transcriber_test_failed",
                    "Transcriber initialization failed. Check server logs.",
                    details={"error_id": error_id},
                ),
            )
