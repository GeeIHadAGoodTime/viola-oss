"""Wake-word training and model-management HTTP API."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from core.user_context import user_scope
from fastapi import Body, Depends, HTTPException, Request, status
from services.violawake_client import (
    ViolaWakeClient,
    ViolaWakeClientError,
    ViolaWakeRemoteError,
    ViolaWakeUnavailableError,
    WakeAudioSample,
)
from services.wake_word.custom_models import (
    DEFAULT_WAKE_MODEL_ID,
    WakeTrainingJobRecord,
    delete_model_for_user,
    list_models_for_user,
    read_job_record,
    resolve_model_for_user,
    store_trained_model,
    validate_onnx_model,
    write_job_record,
)
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth

logger = get_logger(__name__)

_MIN_WAKE_SAMPLES = 5
_MAX_WAKE_SAMPLES = 10
_DEFAULT_TRAINING_EPOCHS = 80
_TEMPORARILY_UNAVAILABLE = "Custom wake-words are temporarily unavailable."
_WAKE_DATA_UPLOAD_CONTROL = "wake_data_upload"

# Reason codes the UI can key its tips content off of. "quality_gate" covers
# ViolaWake's automated post-training quality check (letter-graded A-F); a
# grade-F block does not mean the recordings were bad -- see CL-20260714-4c23.
# "quality metrics" is included because CL-20260714-4c23 quotes the observed
# upstream fragment "...for quality metrics" (from the config.json path the
# raw RuntimeError points at) alongside "quality gate" wording; matching both
# only widens which real failures get the reassuring quality-gate copy below
# -- it cannot loosen the leak guarantee, since both branches only ever return
# one of the two fixed, pre-written safe messages.
_QUALITY_GATE_PATTERN = re.compile(r"quality[ _]gate|quality metrics|\bgrade\s*f\b", re.IGNORECASE)
_TRAINING_FAILURE_MESSAGES: dict[str, str] = {
    "quality_gate": (
        "Training finished, but the resulting model did not pass ViolaWake's automated "
        "quality check. This happens even with good recordings -- see the tips below, "
        "then try training again with the same recordings before you re-record."
    ),
    "generic": "Training did not finish successfully. See the tips below, then try again.",
}

# Returned when ViolaWake refuses the submission itself. Its raw refusal text
# must never reach the browser: Viola authenticates to ViolaWake as one shared
# service account, so ViolaWake's queue-capacity refusals are counted over the
# WHOLE shared queue and describe other users' pending jobs, and its error text
# can carry upstream internals (CL-20260714-4c23). The message below is
# deliberately about the service, not about the user's recordings -- a refusal
# caused by the shared queue is not the user's fault and must not be reported
# as if it were.
_TRAINING_REJECTED_MESSAGE = (
    "Viola could not start training right now. The wake-word training service turned the "
    "request down -- wait a few minutes and try again with the same recordings."
)
_STATUS_UNREADABLE_MESSAGE = "Viola could not read this training job's status. Try again in a moment."


def _client(user_id: str) -> ViolaWakeClient:
    """Build a ViolaWake client bound to the authenticated user.

    The client carries the user's opaque tenant key on every request, so
    ViolaWake can keep its per-user training breaker and pending-job cap
    per-user instead of applying them to the whole shared service account.
    """

    return ViolaWakeClient(user_id=user_id)


async def _wake_training_available() -> bool:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async(_WAKE_DATA_UPLOAD_CONTROL, action="custom_wake_training")
    except (ImportError, KeyError, RuntimeError):
        return False
    return bool(decision.allowed)


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=failure_response(code, message))


async def _wake_training_upload_allowed(user_id: str) -> bool:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async(
            _WAKE_DATA_UPLOAD_CONTROL,
            user_id=user_id,
            action="custom_wake_word_training",
        )
        if not decision.allowed:
            logger.debug("Custom wake-word training blocked: %s", decision.reason)
            return False
        return True
    except Exception:
        logger.exception("Custom wake-word training blocked: operator control check failed")
        return False


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _classify_training_failure(raw_error: str) -> str:
    """Classify a raw upstream training-failure message for display purposes.

    Returns "quality_gate" when the failure looks like ViolaWake's automated
    post-training quality check, otherwise "generic". This is a plain
    substring classification of text ViolaWake already produced for this job
    -- it never alters training/grading behavior or routes model output.
    """

    if _QUALITY_GATE_PATTERN.search(raw_error):
        return "quality_gate"
    return "generic"


def _sanitize_remote_training_error(job_id: str, raw_error: str | None) -> tuple[str | None, str | None]:
    """Return a (safe_message, reason_code) pair for a raw ViolaWake failure.

    ViolaWake's own error text can carry internal detail (container paths,
    stack fragments) that must never reach a browser (CL-20260714-4c23). The
    raw text is logged here for operators; only the mapped, safe message and
    reason code are ever returned to the client.
    """

    if not raw_error:
        return None, None
    logger.debug("ViolaWake job %s reported a failure: %s", job_id, raw_error)
    reason_code = _classify_training_failure(raw_error)
    return _TRAINING_FAILURE_MESSAGES[reason_code], reason_code


async def _samples_from_request(request: Request) -> tuple[str, list[WakeAudioSample]]:
    form = await request.form()
    wake_word = str(form.get("wake_word") or form.get("wake_word_text") or "").strip()
    if not wake_word:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="wake_word is required")

    uploads: list[UploadFile] = []
    for field_name in ("samples", "sample", "file", "files"):
        for item in form.getlist(field_name):
            if isinstance(item, UploadFile):
                uploads.append(item)

    if len(uploads) < _MIN_WAKE_SAMPLES or len(uploads) > _MAX_WAKE_SAMPLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Record 5 to 10 wake-word samples before training.",
        )

    samples: list[WakeAudioSample] = []
    for index, upload in enumerate(uploads, start=1):
        content = await upload.read()
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Audio samples cannot be empty.")
        filename = Path(upload.filename or "sample_%02d.wav" % index).name
        samples.append(
            WakeAudioSample(
                filename=filename,
                content=content,
                content_type=upload.content_type or "audio/wav",
            )
        )
    return wake_word, samples


def _get_active_model_path(user_id: str) -> str:
    from ui.settings_manager import get_settings_manager

    with user_scope(user_id):
        settings_manager = get_settings_manager()
        active = settings_manager.get("wake_word_active_model", "")
        if not active:
            active = settings_manager.get("wake_word_model", "")
    return str(active or "")


def _set_active_model(user_id: str, model_path: str, *, model_name: str, is_default: bool) -> None:
    from ui.settings_manager import get_settings_manager

    with user_scope(user_id):
        settings_manager = get_settings_manager()
        settings_manager.set("wake_word_active_model", model_path, save_immediately=False)
        settings_manager.set("wake_word_model", "" if is_default else model_path, save_immediately=False)
        settings_manager.set("use_custom_wake_word", not is_default, save_immediately=False)
        settings_manager.set("custom_wake_word_name", "" if is_default else model_name, save_immediately=False)
        settings_manager.save()


def _reload_wake_detector(model_path: str) -> dict[str, Any]:
    try:
        from voice.wake_detector.facade import WakeDetectorFacade

        result = WakeDetectorFacade.reload_model(model_path)
        if bool(result.get("reloaded")):
            try:
                from diagnostics.voice_status import get_voice_status, set_voice_status

                status_payload = get_voice_status()
                details = dict(status_payload.get("details") or {})
                resolved_path = str(Path(model_path).expanduser().resolve())
                details.update(
                    {
                        "model_path": resolved_path,
                        "model_exists": Path(resolved_path).is_file(),
                    }
                )
                status_payload["details"] = details
                status_payload["wake_enabled"] = True
                status_payload["wake_engine"] = "violawake"
                set_voice_status(status_payload)
            except (ImportError, OSError, RuntimeError) as exc:
                logger.debug("Wake detector status refresh not available: %s", exc)
        return result
    except (ImportError, AttributeError, RuntimeError) as exc:
        logger.debug("Wake detector reload not available: %s", exc)
        return {"reloaded": False, "reason": "detector_not_running"}


def _custom_model_activation_error(record: Any) -> JSONResponse | None:
    if record.is_default:
        return None
    if not validate_onnx_model(Path(record.model_path)):
        return _json_error(status.HTTP_400_BAD_REQUEST, "invalid_wake_model", "Wake model is not a valid ONNX file.")
    if not record.is_supported:
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_wake_model",
            record.unsupported_reason or "Only TemporalCNN wake models are supported.",
        )
    return None


def _model_rows(user_id: str) -> list[dict[str, Any]]:
    active_model_path = _get_active_model_path(user_id)
    return [asdict(row) for row in list_models_for_user(user_id, active_model_path=active_model_path)]


def register_wake_training_routes(context: ApiContext) -> None:
    """Register wake-word training routes on the feature router."""

    router = context.router

    @router.post(
        "/v1/wake/train",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def start_wake_training(
        request: Request,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        """Submit recorded samples to ViolaWake and return a polling job ID."""

        if not await _wake_training_available():
            return _json_error(status.HTTP_503_SERVICE_UNAVAILABLE, "violawake_unavailable", _TEMPORARILY_UNAVAILABLE)

        try:
            wake_word, samples = await _samples_from_request(request)
        except HTTPException as exc:
            return _json_error(exc.status_code, "invalid_training_request", str(exc.detail))

        client = _client(user_id)
        try:
            submission = await client.submit_training_job(
                wake_word_text=wake_word,
                audio_samples=samples,
                epochs=_DEFAULT_TRAINING_EPOCHS,
            )
        except ViolaWakeUnavailableError:
            return _json_error(status.HTTP_503_SERVICE_UNAVAILABLE, "violawake_unavailable", _TEMPORARILY_UNAVAILABLE)
        except ViolaWakeRemoteError:
            # ViolaWake's own refusal text -- logged for operators, never
            # returned: on the shared service account it can describe another
            # user's queue state, and it can carry upstream internals.
            logger.exception("ViolaWake refused a training submission")
            return _json_error(
                status.HTTP_400_BAD_REQUEST,
                "violawake_training_rejected",
                _TRAINING_REJECTED_MESSAGE,
            )
        except ViolaWakeClientError as exc:
            # Locally raised by the bridge (our own wording, no upstream text).
            return _json_error(status.HTTP_400_BAD_REQUEST, "violawake_training_rejected", str(exc))

        record = WakeTrainingJobRecord(
            job_id=submission.job_id,
            user_id=user_id,
            wake_word=wake_word,
            status=submission.status,
            created_at=_now_iso(),
        )
        write_job_record(record)
        return success_response({"job_id": submission.job_id, "status": submission.status})

    @router.get(
        "/v1/wake/jobs/{job_id}",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def wake_job_status(
        job_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        """Poll a user-owned training job and import the model when complete."""

        local_record = read_job_record(user_id, job_id)
        if local_record is None:
            return _json_error(status.HTTP_404_NOT_FOUND, "wake_job_not_found", "Wake-word training job not found.")

        client = _client(user_id)
        try:
            remote_status = await client.get_job_status(job_id)
        except ViolaWakeUnavailableError:
            return _json_error(status.HTTP_503_SERVICE_UNAVAILABLE, "violawake_unavailable", _TEMPORARILY_UNAVAILABLE)
        except ViolaWakeRemoteError:
            logger.exception("ViolaWake refused a job-status read for job %s", job_id)
            return _json_error(status.HTTP_502_BAD_GATEWAY, "violawake_status_failed", _STATUS_UNREADABLE_MESSAGE)
        except ViolaWakeClientError as exc:
            return _json_error(status.HTTP_502_BAD_GATEWAY, "violawake_status_failed", str(exc))

        # error_reason tracks the *original* upstream failure shape so the UI can
        # key its tips content off it, even after `updated.error` is replaced
        # below by a sanitized/locally-authored message (see
        # _sanitize_remote_training_error: the raw text must never reach the
        # client, so we cannot re-derive the reason from the final message).
        safe_error, error_reason = _sanitize_remote_training_error(job_id, remote_status.error)
        updated = WakeTrainingJobRecord(
            job_id=local_record.job_id,
            user_id=local_record.user_id,
            wake_word=local_record.wake_word,
            status=remote_status.status,
            created_at=local_record.created_at,
            model_id=local_record.model_id,
            model_path=local_record.model_path,
            model_hash=local_record.model_hash,
            remote_model_id=remote_status.model_id or local_record.remote_model_id,
            error=safe_error,
        )

        if remote_status.status == "done" and updated.model_path is None:
            if not remote_status.model_url or not remote_status.model_id:
                updated = WakeTrainingJobRecord(
                    **{**asdict(updated), "status": "failed", "error": "ViolaWake did not return a model URL."}
                )
                error_reason = "generic"
            else:
                try:
                    model_config = await client.get_model_config(remote_status.model_id)
                    model_bytes = await client.download_model(
                        remote_status.model_url,
                        expected_sha256=remote_status.model_hash,
                    )
                    model_hash = hashlib.sha256(model_bytes).hexdigest()
                    if remote_status.model_hash and model_hash != remote_status.model_hash:
                        raise ValueError("Downloaded wake model hash mismatch")
                    model_record = store_trained_model(
                        user_id=user_id,
                        wake_word=local_record.wake_word,
                        model_bytes=model_bytes,
                        model_hash=remote_status.model_hash or model_hash,
                        training_config=model_config.training_config,
                        architecture=model_config.architecture,
                        quality_grade=model_config.quality_grade,
                        d_prime=model_config.d_prime,
                        far_per_hour=model_config.far_per_hour,
                        frr=model_config.frr,
                    )
                    updated = WakeTrainingJobRecord(
                        **{
                            **asdict(updated),
                            "model_id": model_record.model_id,
                            "model_path": model_record.model_path,
                            "model_hash": model_hash,
                            "error": None,
                        }
                    )
                    error_reason = None
                except ViolaWakeRemoteError:
                    # Remote-origin text: log it, return the generic message.
                    logger.exception("ViolaWake refused the model import for job %s", job_id)
                    updated = WakeTrainingJobRecord(
                        **{
                            **asdict(updated),
                            "status": "failed",
                            "error": _TRAINING_FAILURE_MESSAGES["generic"],
                        }
                    )
                    error_reason = "generic"
                except (OSError, ValueError, ViolaWakeClientError, ViolaWakeUnavailableError) as exc:
                    updated = WakeTrainingJobRecord(**{**asdict(updated), "status": "failed", "error": str(exc)})
                    error_reason = "generic"

        write_job_record(updated)
        return success_response(
            {
                "job_id": updated.job_id,
                "status": updated.status,
                "progress": remote_status.progress,
                "model_id": updated.model_id,
                "model_path": updated.model_path,
                "model_hash": updated.model_hash,
                "error": updated.error,
                "error_reason": error_reason if updated.error else None,
            }
        )

    @router.get(
        "/v1/wake/models",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def list_wake_models(user_id: str = Depends(get_current_user_id)) -> Any:
        """Return default and custom wake-word models for the current user."""

        return success_response(
            {
                "models": _model_rows(user_id),
                "active_model_path": _get_active_model_path(user_id),
            }
        )

    @router.post(
        "/v1/wake/activate",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def activate_wake_model(
        body: dict[str, Any] = Body(...),
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        """Activate a user-owned wake-word model and hot-reload the detector."""

        try:
            record = resolve_model_for_user(
                user_id,
                model_id=str(body.get("model_id") or "") or None,
                model_path=str(body.get("model_path") or "") or None,
            )
        except ValueError as exc:
            return _json_error(status.HTTP_403_FORBIDDEN, "wake_model_forbidden", str(exc))

        activation_error = _custom_model_activation_error(record)
        if activation_error is not None:
            return activation_error

        _set_active_model(user_id, record.model_path, model_name=record.name, is_default=record.is_default)
        reload_result = _reload_wake_detector(record.model_path)
        return success_response({"model": asdict(record), "reload": reload_result})

    @router.delete(
        "/v1/wake/models/{model_id}",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def delete_wake_model(
        model_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        """Delete a custom wake-word model owned by the current user."""

        try:
            record = resolve_model_for_user(user_id, model_id=model_id)
        except ValueError:
            return _json_error(status.HTTP_404_NOT_FOUND, "wake_model_not_found", "Wake model not found.")
        if record.is_default:
            return _json_error(
                status.HTTP_400_BAD_REQUEST,
                "default_wake_model_protected",
                "The default Viola wake word cannot be deleted.",
            )

        active_path = _get_active_model_path(user_id)
        try:
            deleted = delete_model_for_user(user_id, model_id)
        except ValueError as exc:
            return _json_error(status.HTTP_400_BAD_REQUEST, "wake_model_delete_failed", str(exc))

        switched_to_default = False
        if active_path and Path(active_path).expanduser().resolve() == Path(record.model_path).expanduser().resolve():
            default_record = resolve_model_for_user(user_id, model_id=DEFAULT_WAKE_MODEL_ID)
            _set_active_model(
                user_id,
                default_record.model_path,
                model_name=default_record.name,
                is_default=True,
            )
            _reload_wake_detector(default_record.model_path)
            switched_to_default = True

        return success_response({"deleted": deleted, "switched_to_default": switched_to_default})

    # Backward-compatible aliases used by older settings UI builds.

    @router.get(
        "/v1/wake/active",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def get_active_wake_word(user_id: str = Depends(get_current_user_id)) -> Any:
        active_path = _get_active_model_path(user_id)
        rows = list_models_for_user(user_id, active_model_path=active_path)
        active = next((row for row in rows if row.is_active), rows[0])
        return success_response({"active": active.name.lower(), "model_path": active.model_path})

    @router.post(
        "/v1/wake/active",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def set_active_wake_word(
        body: dict[str, Any] = Body(...),
        user_id: str = Depends(get_current_user_id),
    ) -> Any:
        wake_word = str(body.get("wake_word", "")).strip().lower()
        if not wake_word:
            return _json_error(status.HTTP_400_BAD_REQUEST, "missing_wake_word", "'wake_word' is required.")
        if wake_word == "viola":
            record = resolve_model_for_user(user_id, model_id=DEFAULT_WAKE_MODEL_ID)
        else:
            matches = [
                row
                for row in list_models_for_user(user_id, active_model_path=_get_active_model_path(user_id))
                if row.name.strip().lower() == wake_word and not row.is_default
            ]
            if not matches:
                return _json_error(status.HTTP_404_NOT_FOUND, "wake_model_not_found", "Wake model not found.")
            record = matches[0]
        activation_error = _custom_model_activation_error(record)
        if activation_error is not None:
            return activation_error
        _set_active_model(user_id, record.model_path, model_name=record.name, is_default=record.is_default)
        reload_result = _reload_wake_detector(record.model_path)
        return success_response({"active": record.name.lower(), "reload": reload_result})

    @router.get(
        "/v1/wake/list",
        tags=["wake"],
        dependencies=[Depends(require_auth)],
    )
    async def list_installed_wake_words(user_id: str = Depends(get_current_user_id)) -> Any:
        rows = list_models_for_user(user_id, active_model_path=_get_active_model_path(user_id))
        wake_words = [
            {
                "name": row.name.lower(),
                "is_builtin": row.is_default,
                "is_active": row.is_active,
                "model_id": row.model_id,
                "metadata": asdict(row),
            }
            for row in rows
        ]
        return success_response({"wake_words": wake_words, "count": len(wake_words)})
