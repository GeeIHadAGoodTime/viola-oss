"""Per-user custom wake-word model storage for Viola."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.wake_config import get_violawake_model_path
from core.logging_config import get_logger
from core.platform import get_project_root
from services.memory.dir import safe_account_id

logger = get_logger(__name__)

DEFAULT_WAKE_MODEL_ID = "default"
DEFAULT_WAKE_WORD_NAME = "Viola"
SUPPORTED_WAKE_MODEL_ARCHITECTURE = "temporal_cnn"
_MODEL_SUFFIX = ".onnx"
_SIDECAR_SUFFIX = ".json"
_MAX_WAKE_MODEL_BYTES = 50 * 1024 * 1024
_MODEL_HASH_CHUNK_BYTES = 1024 * 1024
_SHA256_HEX_LENGTH = 64
_LOWER_HEX_CHARS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class WakeModelRecord:
    """Serializable model metadata returned to API clients."""

    model_id: str
    name: str
    model_path: str
    is_default: bool
    is_active: bool
    size_bytes: int
    created_at: str | None = None
    model_hash: str | None = None
    architecture: str | None = None
    quality_grade: str | None = None
    is_supported: bool = True
    unsupported_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WakeModelInspection:
    """Validated ONNX shape and compatibility information for a wake model."""

    is_valid: bool
    architecture: str | None
    input_rank: int | None
    is_supported: bool
    unsupported_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WakeTrainingJobRecord:
    """Local per-user ownership record for a ViolaWake training job."""

    job_id: str
    user_id: str
    wake_word: str
    status: str
    created_at: str
    model_id: str | None = None
    model_path: str | None = None
    model_hash: str | None = None
    remote_model_id: str | None = None
    error: str | None = None


def trained_models_dir(root: Path | None = None) -> Path:
    """Return the directory used for trained ONNX models."""

    return (root or get_project_root()) / "violawake_data" / "trained_models"


def wake_jobs_dir(root: Path | None = None) -> Path:
    """Return the directory used for per-user wake training job records."""

    return (root or get_project_root()) / "violawake_data" / "wake_jobs"


def wake_word_slug(wake_word: str) -> str:
    """Return a filesystem-safe wake-word slug."""

    slug = re.sub(r"[^a-z0-9_.-]+", "-", wake_word.strip().lower()).strip("-")
    if not slug:
        raise ValueError("wake_word is required")
    return slug


def model_id_for(user_id: str, wake_word: str) -> str:
    """Return the per-user model ID for a wake word."""

    return "%s__%s" % (safe_account_id(user_id), wake_word_slug(wake_word))


def model_path_for_user(user_id: str, wake_word: str, root: Path | None = None) -> Path:
    """Return the ONNX file path for a user's custom wake word."""

    return trained_models_dir(root) / ("%s%s" % (model_id_for(user_id, wake_word), _MODEL_SUFFIX))


def job_path_for_user(user_id: str, job_id: str, root: Path | None = None) -> Path:
    """Return the local job record path."""

    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id).strip())
    if not safe_job_id:
        raise ValueError("job_id is required")
    return wake_jobs_dir(root) / safe_account_id(user_id) / ("%s.json" % safe_job_id)


def default_model_path(root: Path | None = None) -> Path:
    """Return the default product wake model path."""

    if root is None:
        detected = get_violawake_model_path()
        if detected is not None:
            return detected
    return trained_models_dir(root) / "temporal_cnn.onnx"


def write_job_record(record: WakeTrainingJobRecord, root: Path | None = None) -> None:
    """Persist a training job record."""

    path = job_path_for_user(record.user_id, record.job_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True), encoding="utf-8")


def read_job_record(user_id: str, job_id: str, root: Path | None = None) -> WakeTrainingJobRecord | None:
    """Read a training job record if it belongs to the user."""

    path = job_path_for_user(user_id, job_id, root)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("user_id") != user_id:
        return None
    return WakeTrainingJobRecord(**data)


def store_trained_model(
    *,
    user_id: str,
    wake_word: str,
    model_bytes: bytes,
    model_hash: str | None = None,
    training_config: dict[str, Any] | None = None,
    architecture: str | None = None,
    quality_grade: str | None = None,
    d_prime: float | None = None,
    far_per_hour: float | None = None,
    frr: float | None = None,
    root: Path | None = None,
) -> WakeModelRecord:
    """Write, validate, and register a trained model for one user."""

    if not model_bytes:
        raise ValueError("model_bytes is required")
    if len(model_bytes) > _MAX_WAKE_MODEL_BYTES:
        raise ValueError("Downloaded model exceeds the maximum wake-model size")
    actual_model_hash = hashlib.sha256(model_bytes).hexdigest()
    expected_model_hash = _normalize_sha256(model_hash)
    if model_hash is not None and expected_model_hash is None:
        raise ValueError("Downloaded model hash is not a valid SHA-256 digest")
    if expected_model_hash is not None and not hmac.compare_digest(actual_model_hash, expected_model_hash):
        raise ValueError("Downloaded model hash does not match expected SHA-256 digest")

    model_path = model_path_for_user(user_id, wake_word, root)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = model_path.with_suffix("%s.tmp" % model_path.suffix)
    tmp_path.write_bytes(model_bytes)
    try:
        inspection = inspect_wake_model(tmp_path, metadata=training_config)
        if not inspection.is_valid:
            raise ValueError("Downloaded model is not a valid ONNX file")
        expected_architecture = _normalize_architecture(architecture) or _metadata_architecture(training_config)
        if expected_architecture and inspection.architecture and expected_architecture != inspection.architecture:
            raise ValueError(
                "Downloaded wake model architecture mismatch: expected %s but observed %s."
                % (expected_architecture, inspection.architecture)
            )
        if not inspection.is_supported:
            raise ValueError(
                inspection.unsupported_reason or _unsupported_architecture_message(inspection.architecture)
            )
        tmp_path.replace(model_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    created_at = datetime.now(UTC).isoformat()
    resolved_architecture = inspection.architecture or expected_architecture
    resolved_quality_grade = _normalize_quality_grade(quality_grade) or _metadata_quality_grade(training_config)
    metadata = {
        "user_id": user_id,
        "wake_word": wake_word.strip(),
        "model_id": model_path.stem,
        "model_hash": actual_model_hash,
        "created_at": created_at,
        "architecture": resolved_architecture,
        "quality_grade": resolved_quality_grade,
        "training_config": training_config,
        "d_prime": d_prime,
        "far_per_hour": far_per_hour,
        "frr": frr,
    }
    _sidecar_path(model_path).write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return _custom_model_record(model_path, metadata, is_active=False, user_prefix="%s__" % safe_account_id(user_id))


def list_models_for_user(
    user_id: str,
    *,
    active_model_path: str | None = None,
    root: Path | None = None,
) -> list[WakeModelRecord]:
    """List the default model plus custom models owned by one user."""

    active_resolved = _resolve_existing(active_model_path) if active_model_path else None
    default_path = default_model_path(root)
    rows = [
        WakeModelRecord(
            model_id=DEFAULT_WAKE_MODEL_ID,
            name=DEFAULT_WAKE_WORD_NAME,
            model_path=str(default_path),
            is_default=True,
            is_active=_paths_equal(active_resolved, default_path) or active_resolved is None,
            size_bytes=_file_size(default_path),
            architecture=SUPPORTED_WAKE_MODEL_ARCHITECTURE,
            is_supported=True,
        )
    ]

    prefix = "%s__" % safe_account_id(user_id)
    models_dir = trained_models_dir(root)
    if not models_dir.exists():
        return rows

    for model_path in sorted(models_dir.glob("%s*%s" % (prefix, _MODEL_SUFFIX))):
        if not model_path.is_file():
            continue
        metadata = _read_model_metadata(model_path)
        rows.append(
            _custom_model_record(
                model_path, metadata, is_active=_paths_equal(active_resolved, model_path), user_prefix=prefix
            )
        )
    return rows


def resolve_model_for_user(
    user_id: str,
    *,
    model_id: str | None = None,
    model_path: str | None = None,
    root: Path | None = None,
) -> WakeModelRecord:
    """Resolve a default or user-owned model, otherwise raise ValueError."""

    if model_id == DEFAULT_WAKE_MODEL_ID or (not model_id and not model_path):
        path = default_model_path(root)
        return WakeModelRecord(
            model_id=DEFAULT_WAKE_MODEL_ID,
            name=DEFAULT_WAKE_WORD_NAME,
            model_path=str(path),
            is_default=True,
            is_active=False,
            size_bytes=_file_size(path),
            architecture=SUPPORTED_WAKE_MODEL_ARCHITECTURE,
            is_supported=True,
        )

    models_dir = trained_models_dir(root).resolve()
    safe_user = safe_account_id(user_id)
    candidate = _resolve_candidate(model_id=model_id, model_path=model_path, models_dir=models_dir)
    if candidate.stem.startswith("%s__" % safe_user) and _is_child(candidate, models_dir) and candidate.is_file():
        metadata = _read_model_metadata(candidate)
        return _custom_model_record(candidate, metadata, is_active=False, user_prefix="%s__" % safe_user)

    default_path = default_model_path(root)
    if model_path and _paths_equal(candidate, default_path):
        return WakeModelRecord(
            model_id=DEFAULT_WAKE_MODEL_ID,
            name=DEFAULT_WAKE_WORD_NAME,
            model_path=str(default_path),
            is_default=True,
            is_active=False,
            size_bytes=_file_size(default_path),
            architecture=SUPPORTED_WAKE_MODEL_ARCHITECTURE,
            is_supported=True,
        )

    raise ValueError("Wake model is not available for this user")


def delete_model_for_user(user_id: str, model_id: str, root: Path | None = None) -> bool:
    """Delete a user-owned custom model. The default model is never deleted."""

    if model_id == DEFAULT_WAKE_MODEL_ID:
        raise ValueError("The default Viola wake word cannot be deleted")
    record = resolve_model_for_user(user_id, model_id=model_id, root=root)
    if record.is_default:
        raise ValueError("The default Viola wake word cannot be deleted")
    path = Path(record.model_path)
    if not path.exists():
        return False
    path.unlink()
    sidecar = _sidecar_path(path)
    if sidecar.exists():
        sidecar.unlink()
    return True


def validate_onnx_model(path: Path) -> bool:
    """Validate that an ONNX model exists, is readable, and passes ONNX checks."""

    if not path.is_file():
        return False
    try:
        _probe_onnx_model(path)
        return True
    except Exception as exc:
        logger.warning("Wake model validation failed for %s: %s", path, exc)
        return False


def inspect_wake_model(path: Path, metadata: dict[str, Any] | None = None) -> WakeModelInspection:
    """Return ONNX validity plus the production-compatibility verdict."""

    if not path.is_file():
        return WakeModelInspection(
            is_valid=False,
            architecture=None,
            input_rank=None,
            is_supported=False,
            unsupported_reason="Wake model file is missing.",
        )

    try:
        input_rank, _ = _probe_onnx_model(path)
    except Exception as exc:
        logger.warning("Wake model inspection failed for %s: %s", path, exc)
        return WakeModelInspection(
            is_valid=False,
            architecture=_metadata_architecture(metadata),
            input_rank=None,
            is_supported=False,
            unsupported_reason="Wake model is not a valid ONNX file.",
        )

    declared_architecture = _metadata_architecture(metadata)
    observed_architecture = _architecture_for_input_rank(input_rank)
    if declared_architecture and observed_architecture and declared_architecture != observed_architecture:
        return WakeModelInspection(
            is_valid=True,
            architecture=observed_architecture,
            input_rank=input_rank,
            is_supported=False,
            unsupported_reason=(
                "Wake model architecture mismatch: declared %s but observed %s."
                % (declared_architecture, observed_architecture)
            ),
        )

    architecture = declared_architecture or observed_architecture
    if architecture == SUPPORTED_WAKE_MODEL_ARCHITECTURE:
        return WakeModelInspection(
            is_valid=True,
            architecture=architecture,
            input_rank=input_rank,
            is_supported=True,
        )

    return WakeModelInspection(
        is_valid=True,
        architecture=architecture,
        input_rank=input_rank,
        is_supported=False,
        unsupported_reason=_unsupported_architecture_message(architecture),
    )


def _resolve_candidate(*, model_id: str | None, model_path: str | None, models_dir: Path) -> Path:
    if model_path:
        return Path(model_path).expanduser().resolve()
    if model_id:
        safe_model_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_id.strip())
        return (models_dir / ("%s%s" % (safe_model_id, _MODEL_SUFFIX))).resolve()
    raise ValueError("model_id or model_path is required")


def _read_model_metadata(model_path: Path) -> dict[str, Any]:
    sidecar = _sidecar_path(model_path)
    if not sidecar.exists():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read wake model metadata %s: %s", sidecar, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _custom_model_record(
    model_path: Path, metadata: dict[str, Any], *, is_active: bool, user_prefix: str
) -> WakeModelRecord:
    inspection = inspect_wake_model(model_path, metadata=metadata)
    integrity_error = _metadata_integrity_error(model_path, metadata)
    return WakeModelRecord(
        model_id=model_path.stem,
        name=str(metadata.get("wake_word") or model_path.stem.removeprefix(user_prefix)),
        model_path=str(model_path),
        is_default=False,
        is_active=is_active,
        size_bytes=_file_size(model_path),
        created_at=_optional_str(metadata.get("created_at")),
        model_hash=_optional_str(metadata.get("model_hash")),
        architecture=_metadata_architecture(metadata) or inspection.architecture,
        quality_grade=_metadata_quality_grade(metadata),
        is_supported=inspection.is_supported and integrity_error is None,
        unsupported_reason=integrity_error or inspection.unsupported_reason,
    )


def _sidecar_path(model_path: Path) -> Path:
    return model_path.with_suffix(_SIDECAR_SUFFIX)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _resolve_existing(path: str | None) -> Path | None:
    if not path:
        return None
    try:
        candidate = Path(path).expanduser()
        if candidate.exists():
            return candidate.resolve()
    except OSError:
        return None
    return None


def _paths_equal(left: Path | None, right: Path) -> bool:
    if left is None:
        return False
    try:
        return left.resolve() == right.expanduser().resolve()
    except OSError:
        return False


def _is_child(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _probe_onnx_model(path: Path) -> tuple[int, tuple[object, ...]]:
    model_size = path.stat().st_size
    if model_size <= 0:
        raise ValueError("Wake model file is empty")
    if model_size > _MAX_WAKE_MODEL_BYTES:
        raise ValueError("Wake model exceeds the maximum allowed size")
    try:
        import onnx

        model = onnx.load(str(path))
        onnx.checker.check_model(model)
        if not model.graph.input:
            raise ValueError("Wake model has no graph inputs")
    except ImportError:
        pass

    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    if not inputs:
        raise ValueError("Wake model has no runtime inputs")
    shape = tuple(inputs[0].shape)
    return len(shape), shape


def _metadata_integrity_error(model_path: Path, metadata: dict[str, Any]) -> str | None:
    if not metadata:
        return "Wake model metadata sidecar is missing."
    expected_hash = _normalize_sha256(metadata.get("model_hash"))
    if expected_hash is None:
        return "Wake model metadata is missing a valid SHA-256 hash."
    try:
        actual_hash = _sha256_file(model_path)
    except OSError:
        return "Wake model file could not be read for integrity verification."
    if not hmac.compare_digest(actual_hash, expected_hash):
        return "Wake model integrity check failed."
    return None


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_MODEL_HASH_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _normalize_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.strip().lower()
    if len(digest) != _SHA256_HEX_LENGTH:
        return None
    if any(char not in _LOWER_HEX_CHARS for char in digest):
        return None
    return digest


def _architecture_for_input_rank(input_rank: int | None) -> str | None:
    if input_rank == 3:
        return SUPPORTED_WAKE_MODEL_ARCHITECTURE
    if input_rank == 2:
        return "mlp_on_oww"
    return None


def _metadata_architecture(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    architecture = _normalize_architecture(metadata.get("architecture"))
    if architecture:
        return architecture
    training_config = metadata.get("training_config")
    if isinstance(training_config, dict):
        return _normalize_architecture(training_config.get("architecture"))
    return None


def _metadata_quality_grade(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    quality_grade = _normalize_quality_grade(metadata.get("quality_grade"))
    if quality_grade:
        return quality_grade
    training_config = metadata.get("training_config")
    if isinstance(training_config, dict):
        quality_grade = _normalize_quality_grade(training_config.get("quality_grade"))
        if quality_grade:
            return quality_grade
        quality_gate = training_config.get("quality_gate")
        if isinstance(quality_gate, dict):
            return _normalize_quality_grade(quality_gate.get("grade"))
    return None


def _normalize_architecture(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    architecture = value.strip().lower()
    if not architecture:
        return None
    if architecture in {"temporalcnn", "temporal_cnn"}:
        return SUPPORTED_WAKE_MODEL_ARCHITECTURE
    if architecture in {"mlp", "mlp_on_oww"}:
        return "mlp_on_oww"
    return architecture


def _normalize_quality_grade(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    grade = value.strip().upper()
    return grade or None


def _unsupported_architecture_message(architecture: str | None) -> str:
    if architecture == "mlp_on_oww":
        return (
            "Legacy MLP wake models are unsupported. Retrain this wake word with the production TemporalCNN pipeline."
        )
    if architecture:
        return "Wake model architecture '%s' is unsupported. Only TemporalCNN models are allowed." % architecture
    return "Wake model architecture is unsupported. Only TemporalCNN models are allowed."


__all__ = [
    "DEFAULT_WAKE_MODEL_ID",
    "WakeModelInspection",
    "WakeModelRecord",
    "WakeTrainingJobRecord",
    "delete_model_for_user",
    "inspect_wake_model",
    "job_path_for_user",
    "list_models_for_user",
    "model_id_for",
    "model_path_for_user",
    "read_job_record",
    "resolve_model_for_user",
    "store_trained_model",
    "trained_models_dir",
    "validate_onnx_model",
    "wake_word_slug",
    "write_job_record",
]
