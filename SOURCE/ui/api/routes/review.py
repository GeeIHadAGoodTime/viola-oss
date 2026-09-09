"""Review API routes for wake word data collection clips.

Provides endpoints for viewing, classifying, and exporting
wake word audio clips for model improvement.
"""

from __future__ import annotations

import csv
import io
import stat
import zipfile
from pathlib import Path

from fastapi.responses import JSONResponse, StreamingResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends, Query
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox

log = get_logger(__name__)
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def _contains_reparse_component(root: Path, candidate: Path) -> bool:
    """Return true when a candidate path traverses a symlink or Windows junction."""
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True

    current = root
    for part in relative.parts:
        current = current / part
        try:
            file_stat = current.lstat()
        except FileNotFoundError:
            continue
        if current.is_symlink():
            return True
        if _REPARSE_POINT_ATTRIBUTE and getattr(file_stat, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE:
            return True
    return False


def _clip_audio_path(data_dir: Path, clip: dict) -> Path:
    if clip["anonymized"]:
        return data_dir / "anonymized" / ("%d.wav" % clip["id"])
    return data_dir / clip["audio_path"]


def _resolve_clip_audio_path(data_dir: Path, clip: dict) -> Path | None:
    resolved_data_dir = data_dir.resolve()
    raw_path = _clip_audio_path(resolved_data_dir, clip)
    resolved_audio_path = raw_path.resolve(strict=False)
    if not resolved_audio_path.is_relative_to(resolved_data_dir):
        return None
    if _contains_reparse_component(resolved_data_dir, raw_path):
        return None
    return resolved_audio_path


def _read_contained_audio_bytes(data_dir: Path, audio_path: Path) -> bytes | None:
    resolved_data_dir = data_dir.resolve()
    if not audio_path.exists() or not audio_path.is_file():
        return None
    if _contains_reparse_component(resolved_data_dir, audio_path):
        raise ValueError("invalid_audio_path")
    payload = audio_path.read_bytes()
    if _contains_reparse_component(resolved_data_dir, audio_path):
        raise ValueError("invalid_audio_path")
    return payload


def register_review_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register review API endpoints."""
    router = context.router

    @router.get("/v1/review/stats", dependencies=[Depends(require_auth)])
    async def get_review_stats():
        """Get clip statistics and storage usage."""
        try:
            from voice.wake_detector.data_collection.database import (
                get_data_collection_db,
            )
            from voice.wake_detector.data_collection.storage_manager import (
                get_storage_manager,
            )

            db = get_data_collection_db()
            stats = db.get_stats()
            storage = get_storage_manager().get_storage_stats()
            return success_response(data={**stats, "storage": storage})
        except Exception:
            log.exception("Failed to get review stats")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "review_stats_unavailable",
                    "Review statistics are temporarily unavailable.",
                ),
            )

    @router.get("/v1/review/clips", dependencies=[Depends(require_auth)])
    async def get_review_clips(
        classification: str | None = Query(default=None),
        reviewed: bool | None = Query(default=None),
        min_score: float | None = Query(default=None),
        max_score: float | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        """Get paginated list of clips with filters."""
        try:
            from voice.wake_detector.data_collection.database import (
                get_data_collection_db,
            )

            db = get_data_collection_db()
            clips = db.get_clips(
                classification=classification,
                reviewed=reviewed,
                min_score=min_score,
                max_score=max_score,
                limit=limit,
                offset=offset,
            )
            return success_response(data={"clips": clips, "count": len(clips)})
        except Exception:
            log.exception("Failed to get clips")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "review_clips_unavailable",
                    "Review clips are temporarily unavailable.",
                ),
            )

    @router.get("/v1/review/clips/{clip_id}/audio", dependencies=[Depends(require_auth)])
    async def get_clip_audio(clip_id: int):
        """Serve WAV audio file for a clip."""
        try:
            from config.settings import settings
            from voice.wake_detector.data_collection.database import (
                get_data_collection_db,
            )

            db = get_data_collection_db()
            clip = db.get_clip(clip_id)
            if clip is None:
                return JSONResponse(
                    status_code=404,
                    content=failure_response(
                        "clip_not_found",
                        "No review clip with that ID was found.",
                    ),
                )

            data_dir = Path(settings.data_dir) / "wake_clips"
            audio_path = _resolve_clip_audio_path(data_dir, clip)

            if audio_path is None:
                return JSONResponse(
                    status_code=400,
                    content=failure_response(
                        "invalid_audio_path",
                        "Invalid audio path.",
                    ),
                )

            try:
                audio_bytes = _read_contained_audio_bytes(data_dir, audio_path)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content=failure_response(
                        "invalid_audio_path",
                        "Invalid audio path.",
                    ),
                )

            if audio_bytes is None:
                return JSONResponse(
                    status_code=404,
                    content=failure_response(
                        "clip_audio_not_found",
                        "The audio file for this clip was not found.",
                    ),
                )

            return StreamingResponse(
                io.BytesIO(audio_bytes),
                media_type="audio/wav",
                headers={"Content-Disposition": "inline; filename=clip_%d.wav" % clip_id},
            )
        except Exception:
            log.exception("Failed to serve clip audio %d", clip_id)
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "review_audio_unavailable",
                    "Review audio is temporarily unavailable.",
                ),
            )

    @router.patch("/v1/review/clips/{clip_id}", dependencies=[Depends(require_auth)])
    async def update_clip(clip_id: int, body: dict):
        """Update clip classification or mark as reviewed."""
        try:
            from voice.wake_detector.data_collection.database import (
                get_data_collection_db,
            )

            db = get_data_collection_db()
            clip = db.get_clip(clip_id)
            if clip is None:
                return JSONResponse(
                    status_code=404,
                    content=failure_response(
                        "clip_not_found",
                        "No review clip with that ID was found.",
                    ),
                )

            classification = body.get("classification")
            review_result = body.get("review_result")

            if review_result:
                db.mark_reviewed(clip_id, review_result, classification)
            elif classification:
                db.update_classification(clip_id, classification, "manual_review")

            updated = db.get_clip(clip_id)
            return success_response(data=updated)
        except Exception:
            log.exception("Failed to update clip %d", clip_id)
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "review_update_failed",
                    "Try again in a moment",
                ),
            )

    @router.post("/v1/review/clips/batch", dependencies=[Depends(require_auth)])
    async def batch_update_clips(body: dict):
        """Batch operations: confirm, reclassify, or discard clips."""
        try:
            from voice.wake_detector.data_collection.database import (
                get_data_collection_db,
            )

            db = get_data_collection_db()
            action = body.get("action")
            clip_ids = body.get("clip_ids", [])

            if not clip_ids or not action:
                return JSONResponse(
                    status_code=400,
                    content=failure_response(
                        "invalid_input",
                        "The request must include an action and at least one clip ID.",
                    ),
                )

            count = 0
            if action == "confirm":
                for cid in clip_ids:
                    if db.mark_reviewed(cid, "confirmed"):
                        count += 1
            elif action == "discard":
                count = db.delete_clips_by_ids(clip_ids)
            elif action == "reclassify":
                new_class = body.get("classification")
                if not new_class:
                    return JSONResponse(
                        status_code=400,
                        content=failure_response(
                            "invalid_input",
                            "A classification is required for reclassify.",
                        ),
                    )
                for cid in clip_ids:
                    if db.mark_reviewed(cid, "reclassified", new_class):
                        count += 1
            else:
                return JSONResponse(
                    status_code=400,
                    content=failure_response(
                        "invalid_action",
                        "The requested batch action is not supported.",
                    ),
                )

            return success_response(data={"affected": count})
        except Exception:
            log.exception("Batch update failed")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "batch_update_failed",
                    "Try again in a moment",
                ),
            )

    @router.get("/v1/review/export", dependencies=[Depends(require_auth)])
    async def export_training_data():
        """Export training dataset as zip (audio files + labels.csv)."""
        try:
            from config.settings import settings
            from voice.wake_detector.data_collection.database import (
                get_data_collection_db,
            )

            db = get_data_collection_db()
            clips = db.get_clips(limit=10000)

            if not clips:
                return JSONResponse(
                    status_code=404,
                    content=failure_response(
                        "no_clips_available",
                        "There are no review clips available to export.",
                    ),
                )

            data_dir = Path(settings.data_dir) / "wake_clips"
            buf = io.BytesIO()

            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                # Write labels.csv
                csv_buf = io.StringIO()
                writer = csv.writer(csv_buf)
                writer.writerow(
                    [
                        "id",
                        "timestamp",
                        "confidence_score",
                        "classification",
                        "classification_method",
                        "reviewed",
                        "filename",
                    ]
                )

                for clip in clips:
                    audio_path = _resolve_clip_audio_path(data_dir, clip)

                    filename = "clip_%d.wav" % clip["id"]

                    if audio_path is None:
                        log.warning("Skipping clip %d: path traversal detected", clip["id"])
                        continue

                    try:
                        audio_bytes = _read_contained_audio_bytes(data_dir, audio_path)
                    except ValueError:
                        log.warning("Skipping clip %d: reparse path detected", clip["id"])
                        continue
                    if audio_bytes is None:
                        continue
                    zf.writestr("audio/%s" % filename, audio_bytes)

                    writer.writerow(
                        [
                            clip["id"],
                            clip["timestamp"],
                            clip["confidence_score"],
                            clip["classification"],
                            clip["classification_method"],
                            clip["reviewed"],
                            filename,
                        ]
                    )

                zf.writestr("labels.csv", csv_buf.getvalue())

            buf.seek(0)
            return StreamingResponse(
                buf,
                media_type="application/zip",
                headers={
                    "Content-Disposition": "attachment; filename=wake_training_data.zip",
                },
            )
        except Exception:
            log.exception("Export failed")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "review_export_failed",
                    "Try again in a moment",
                ),
            )

    @router.get("/v1/review/trends", dependencies=[Depends(require_auth)])
    async def get_trends(days: int = Query(default=30, ge=1, le=365)):
        """Get daily classification trends."""
        try:
            from voice.wake_detector.data_collection.database import (
                get_data_collection_db,
            )

            db = get_data_collection_db()
            trends = db.get_daily_trends(days)
            return success_response(data={"trends": trends})
        except Exception:
            log.exception("Failed to get trends")
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "review_trends_unavailable",
                    "Review trends are temporarily unavailable.",
                ),
            )
