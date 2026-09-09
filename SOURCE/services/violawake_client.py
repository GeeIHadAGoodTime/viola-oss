"""Internal client for ViolaWake wake-word training service.

ViolaWake stays a privileged backend dependency: browser clients call Viola,
and Viola calls the ViolaWake API with a service credential stored in env.

The service credential authenticates ONE synthetic ViolaWake account ("Viola
Service", see docker-compose.cloud.yml) shared by every Viola install. Left
unqualified, that collapses ViolaWake's per-user controls -- its per-user
training circuit breaker and its per-user pending-job cap (CL-20260717-9bc3)
-- into one global control for the whole Viola install base: one user's three
failed trainings trip the breaker and strand everyone's jobs, and one user's
queued jobs consume the shared pending cap so the next user is rejected
because of a stranger's queue.

So every request carries ``X-Viola-Tenant``: a stable, opaque per-user
partition key derived one-way from the Viola ``user_id``. It gives ViolaWake
exactly what it needs to keep those per-user controls per-user -- a partition
key -- and nothing else: the digest is one-way, so no Viola account
identifier crosses the boundary. Attribution is bound to the CLIENT, not to
individual calls, so a user-scoped ViolaWake request cannot be constructed
without naming the user it belongs to (Multi-Tenant Rule: per-user by
default, and a missing ``user_id`` fails loudly instead of falling back to a
shared identity).

ViolaWake honoring the header is the upstream half of the fix; until it does,
the header is inert and this client behaves exactly as before.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from config import env
from core.constants import TIMEOUT_VERY_LONG

DEFAULT_VIOLAWAKE_API_URL = "http://backend:8000"
_DEFAULT_TIMEOUT = TIMEOUT_VERY_LONG
_MAX_MODEL_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MODEL_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_SHA256_HEX_LENGTH = 64
_LOWER_HEX_CHARS = frozenset("0123456789abcdef")
_WAKE_DATA_UPLOAD_CONTROL = "wake_data_upload"

# Per-user partition key carried on every ViolaWake request. Version-prefixed
# so the derivation can change without silently re-partitioning live queues.
WAKE_TENANT_HEADER = "X-Viola-Tenant"
_TENANT_DIGEST_PREFIX = "violawake-tenant:v1:"
_TENANT_KEY_LENGTH = 32


def wake_tenant_key(user_id: str) -> str:
    """Return the stable, opaque ViolaWake partition key for a Viola user.

    One-way (SHA-256 over a domain-separated string), so ViolaWake receives a
    partition key it can scope its per-user breaker and pending cap on without
    ever receiving a Viola account identifier. Stable for a given user across
    restarts and installs, and distinct per user -- the two properties the
    upstream controls need.

    Raises ValueError on a missing user_id: a ViolaWake request that cannot
    name its user must fail, never fall back to the shared service identity.
    """

    cleaned = (user_id or "").strip()
    if not cleaned:
        raise ValueError("user_id is required to scope a ViolaWake request")
    digest = hashlib.sha256((_TENANT_DIGEST_PREFIX + cleaned).encode("utf-8")).hexdigest()
    return digest[:_TENANT_KEY_LENGTH]


class ViolaWakeClientError(RuntimeError):
    """Base error for ViolaWake bridge failures."""


class ViolaWakeRemoteError(ViolaWakeClientError):
    """Raised when ViolaWake itself rejected the request.

    The message is ViolaWake's own text. On the shared service account that
    text can describe state belonging to OTHER Viola users (its pending-job
    cap counts the whole shared queue, so its rejection is about strangers'
    jobs) and can carry upstream internals (container paths, stack fragments
    -- CL-20260714-4c23). Callers must sanitize before it reaches a browser;
    the distinct type is what lets a caller tell "ViolaWake said this" from
    "we said this" without parsing the text.
    """


class ViolaWakeUnavailableError(ViolaWakeClientError):
    """Raised when ViolaWake cannot be reached or is not configured."""


@dataclass(frozen=True, slots=True)
class WakeAudioSample:
    """Audio sample submitted to ViolaWake."""

    filename: str
    content: bytes
    content_type: str = "audio/wav"


@dataclass(frozen=True, slots=True)
class TrainingSubmission:
    """Normalized training-submission response."""

    job_id: str
    status: str


@dataclass(frozen=True, slots=True)
class TrainingJobStatus:
    """Normalized training-job status from ViolaWake."""

    job_id: str
    status: str
    progress: float | None = None
    model_url: str | None = None
    model_hash: str | None = None
    model_id: str | None = None
    error: str | None = None
    raw_status: str | None = None


@dataclass(frozen=True, slots=True)
class TrainingModelConfig:
    """Normalized training metadata for a trained wake-word model."""

    d_prime: float | None = None
    far_per_hour: float | None = None
    frr: float | None = None
    training_config: dict[str, Any] | None = None
    architecture: str | None = None
    quality_grade: str | None = None


class ViolaWakeClient:
    """Thin async HTTP client for the ViolaWake Console API.

    Bound to one Viola user for its whole lifetime: ``user_id`` is required,
    and every request the client issues carries that user's opaque tenant key
    (see the module docstring). Construct one client per user, per request
    scope -- never a shared, user-less instance.
    """

    def __init__(
        self,
        *,
        user_id: str,
        api_url: str | None = None,
        service_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
        max_model_download_bytes: int = _MAX_MODEL_DOWNLOAD_BYTES,
    ) -> None:
        # Derived up front so a user-less client cannot be built at all -- the
        # failure lands at construction, not at some later request that would
        # otherwise have gone out under the shared service identity.
        self._tenant_key = wake_tenant_key(user_id)
        self.api_url = (api_url or env.get("VIOLA_WAKEWORD_API_URL", DEFAULT_VIOLAWAKE_API_URL) or "").rstrip("/")
        self._service_key = service_key if service_key is not None else (env.get("VIOLA_WAKEWORD_SERVICE_KEY") or "")
        self._timeout = timeout
        self._http_client = http_client
        self._max_model_download_bytes = max(1, int(max_model_download_bytes))

    @property
    def configured(self) -> bool:
        """Return true when the client has enough config to call ViolaWake."""

        return bool(self.api_url and self._service_key)

    @property
    def tenant_key(self) -> str:
        """Return the opaque per-user partition key sent to ViolaWake."""

        return self._tenant_key

    async def submit_training_job(
        self,
        wake_word_text: str,
        audio_samples: list[WakeAudioSample],
        *,
        epochs: int = 80,
    ) -> TrainingSubmission:
        """Upload samples and enqueue a ViolaWake training job.

        The job is attributed to the user this client was constructed for; it
        takes no ``user_id`` of its own, so there is no second place that can
        disagree about whose job this is.
        """

        wake_word = wake_word_text.strip().lower()
        if not wake_word:
            raise ViolaWakeClientError("wake_word_text is required")
        if not audio_samples:
            raise ViolaWakeClientError("at least one audio sample is required")

        upload_payload = await self._post_multipart(
            "/api/recordings/bulk-upload",
            data={"wake_word": wake_word},
            files=[
                (
                    "file",
                    (
                        sample.filename,
                        sample.content,
                        sample.content_type or "application/octet-stream",
                    ),
                )
                for sample in audio_samples
            ],
        )
        recording_ids = _recording_ids_from_bulk_upload(upload_payload)
        if len(recording_ids) != len(audio_samples):
            raise ViolaWakeClientError("ViolaWake rejected one or more audio samples")

        response = await self._post_json(
            "/api/jobs",
            {
                "wake_word": wake_word,
                "recording_ids": recording_ids,
                "epochs": epochs,
            },
        )
        job_id = str(response.get("job_id", "")).strip()
        if not job_id:
            raise ViolaWakeClientError("ViolaWake did not return a job_id")
        return TrainingSubmission(job_id=job_id, status=_normalize_status(str(response.get("status", "queued"))))

    async def get_job_status(self, job_id: str) -> TrainingJobStatus:
        """Poll a ViolaWake training job."""

        response = await self._get_json("/api/jobs/%s" % job_id)
        raw_status = str(response.get("status", "") or "")
        status = _normalize_status(raw_status)
        model_id = response.get("model_id")
        model_url = "/api/models/%s/download" % model_id if model_id is not None and status == "done" else None
        return TrainingJobStatus(
            job_id=str(response.get("job_id", job_id)),
            status=status,
            progress=_optional_float(response.get("progress_pct")),
            model_url=model_url,
            model_hash=_optional_sha256(
                response.get("model_hash") or response.get("sha256") or response.get("model_sha256")
            ),
            model_id=str(model_id) if model_id is not None else None,
            error=response.get("error") if isinstance(response.get("error"), str) else None,
            raw_status=raw_status,
        )

    async def download_model(self, model_url: str, *, expected_sha256: str | None = None) -> bytes:
        """Download a trained ONNX model from ViolaWake."""

        expected_hash = _optional_sha256(expected_sha256)
        if expected_sha256 and expected_hash is None:
            raise ViolaWakeClientError("expected model hash is not a valid SHA-256 digest")
        await _require_wake_data_upload_allowed(_control_action("GET", model_url))

        headers, url = self._prepare_request(model_url)
        try:
            if self._http_client is not None:
                return await self._download_model_with_client(
                    self._http_client,
                    url,
                    headers=headers,
                    expected_sha256=expected_hash,
                )
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await self._download_model_with_client(
                    client,
                    url,
                    headers=headers,
                    expected_sha256=expected_hash,
                )
        except httpx.RequestError as exc:
            raise ViolaWakeUnavailableError("ViolaWake is temporarily unavailable") from exc

    async def get_model_config(self, model_id: str) -> TrainingModelConfig:
        """Fetch persisted training metadata for a trained model."""

        response = await self._get_json("/api/models/%s/config" % model_id)
        training_config = response.get("training_config")
        config_payload = training_config if isinstance(training_config, dict) else None
        return TrainingModelConfig(
            d_prime=_optional_float(response.get("d_prime")),
            far_per_hour=_optional_float(response.get("far_per_hour")),
            frr=_optional_float(response.get("frr")),
            training_config=config_payload,
            architecture=_optional_str(response.get("architecture")) or _config_architecture(config_payload),
            quality_grade=_optional_str(response.get("quality_grade")) or _config_quality_grade(config_payload),
        )

    async def model_sha256(self, model_url: str) -> str:
        """Download a model and return its SHA-256 hash."""

        return hashlib.sha256(await self.download_model(model_url)).hexdigest()

    async def _get_json(self, path: str) -> dict[str, Any]:
        response = await self._request("GET", path)
        return _json_object(response)

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._request("POST", path, json=payload)
        return _json_object(response)

    async def _post_multipart(
        self,
        path: str,
        *,
        data: dict[str, str],
        files: list[tuple[str, tuple[str, bytes, str]]],
    ) -> dict[str, Any]:
        response = await self._request("POST", path, data=data, files=files)
        return _json_object(response)

    async def _request(self, method: str, path_or_url: str, **kwargs: Any) -> httpx.Response:
        await _require_wake_data_upload_allowed(_control_action(method, path_or_url))
        headers, url = self._prepare_request(path_or_url, headers=kwargs.pop("headers", None))

        try:
            if self._http_client is not None:
                response = await self._http_client.request(method, url, headers=headers, **kwargs)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise ViolaWakeUnavailableError("ViolaWake is temporarily unavailable") from exc

        _raise_for_response(response)
        return response

    def _prepare_request(self, path_or_url: str, headers: dict[str, str] | None = None) -> tuple[dict[str, str], str]:
        if not self.api_url:
            raise ViolaWakeUnavailableError("ViolaWake API URL is not configured")
        if not self._service_key:
            raise ViolaWakeUnavailableError("ViolaWake service key is not configured")

        request_headers = dict(headers or {})
        request_headers["Authorization"] = _authorization_header(self._service_key)
        # Single choke point: both _request and download_model resolve their
        # headers here, so no outbound ViolaWake call can skip attribution.
        request_headers[WAKE_TENANT_HEADER] = self._tenant_key
        return request_headers, self._resolve_url(path_or_url)

    def _resolve_url(self, path_or_url: str) -> str:
        parsed = urlsplit(path_or_url)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise ViolaWakeClientError("model URL must use http or https")
            if _origin(path_or_url) != _origin(self.api_url):
                raise ViolaWakeClientError("model URL is outside the configured ViolaWake API")
            return path_or_url
        return "%s/%s" % (self.api_url, path_or_url.lstrip("/"))

    async def _download_model_with_client(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        expected_sha256: str | None,
    ) -> bytes:
        hasher = hashlib.sha256()
        downloaded = 0
        chunks: list[bytes] = []

        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code >= 400:
                await response.aread()
                _raise_for_response(response)
            content_length = _optional_int(response.headers.get("content-length"))
            if content_length is not None and content_length > self._max_model_download_bytes:
                raise ViolaWakeClientError("model download exceeds size limit")

            async for chunk in response.aiter_bytes(chunk_size=_MODEL_DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > self._max_model_download_bytes:
                    raise ViolaWakeClientError("model download exceeds size limit")
                hasher.update(chunk)
                chunks.append(chunk)

        actual_hash = hasher.hexdigest()
        if expected_sha256 and actual_hash != expected_sha256:
            raise ViolaWakeClientError("model hash mismatch")
        return b"".join(chunks)


def _recording_ids_from_bulk_upload(payload: dict[str, Any]) -> list[int]:
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ViolaWakeClientError("Invalid ViolaWake upload response")
    recording_ids: list[int] = []
    failures: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "success" and item.get("recording_id") is not None:
            recording_ids.append(int(item["recording_id"]))
        elif item.get("error"):
            failures.append(str(item["error"]))
    if failures:
        # ViolaWake's own per-sample text -- remote-origin, so it is typed as
        # such and must be sanitized before reaching a browser.
        raise ViolaWakeRemoteError("; ".join(failures))
    return recording_ids


def _control_action(method: str, path_or_url: str) -> str:
    path = path_or_url.split("?", 1)[0].rstrip("/")
    if path.endswith("/api/recordings/bulk-upload"):
        return "custom_wake_sample_upload"
    if "/api/models/" in path and path.endswith("/download"):
        return "custom_wake_model_download"
    if "/api/models/" in path and path.endswith("/config"):
        return "custom_wake_model_config"
    if path.endswith("/api/jobs") or "/api/jobs/" in path:
        return "custom_wake_training_job"
    return "custom_wake_violawake_request_%s" % method.lower()


async def _require_wake_data_upload_allowed(action: str) -> None:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async(_WAKE_DATA_UPLOAD_CONTROL, action=action)
    except Exception as exc:
        raise ViolaWakeUnavailableError("Wake data upload safety control is unavailable") from exc
    if not decision.allowed:
        raise ViolaWakeUnavailableError(decision.public_message or "Wake data upload is disabled")


def _normalize_status(status: str) -> str:
    status_value = status.strip().lower()
    if status_value == "pending":
        return "queued"
    if status_value == "running":
        return "training"
    if status_value == "completed":
        return "done"
    if status_value in {"failed", "cancelled"}:
        return "failed"
    if status_value in {"queued", "training", "done"}:
        return status_value
    return "queued"


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ViolaWakeClientError("ViolaWake returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise ViolaWakeClientError("ViolaWake returned an invalid response")
    return payload


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error")
            if isinstance(message, str) and message:
                return message
    return "ViolaWake request failed"


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.strip().lower()
    if len(digest) != _SHA256_HEX_LENGTH:
        return None
    if any(char not in _LOWER_HEX_CHARS for char in digest):
        return None
    return digest


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ViolaWakeClientError("model URL must use http or https")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ViolaWakeClientError("model URL has an invalid port") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def _raise_for_response(response: httpx.Response) -> None:
    if response.status_code >= 500:
        raise ViolaWakeUnavailableError("ViolaWake is temporarily unavailable")
    if response.status_code >= 400:
        raise ViolaWakeRemoteError(_error_message(response))


def _config_architecture(config: dict[str, Any] | None) -> str | None:
    if not config:
        return None
    architecture = config.get("architecture")
    return architecture if isinstance(architecture, str) and architecture else None


def _config_quality_grade(config: dict[str, Any] | None) -> str | None:
    if not config:
        return None
    quality_grade = config.get("quality_grade")
    if isinstance(quality_grade, str) and quality_grade:
        return quality_grade
    quality_gate = config.get("quality_gate")
    if isinstance(quality_gate, dict):
        grade = quality_gate.get("grade")
        if isinstance(grade, str) and grade:
            return grade
    return None


def _authorization_header(service_key: str) -> str:
    key = service_key.strip()
    if key.lower().startswith("bearer "):
        return key
    return "Bearer %s" % key


__all__ = [
    "DEFAULT_VIOLAWAKE_API_URL",
    "WAKE_TENANT_HEADER",
    "TrainingJobStatus",
    "TrainingModelConfig",
    "TrainingSubmission",
    "ViolaWakeClient",
    "ViolaWakeClientError",
    "ViolaWakeRemoteError",
    "ViolaWakeUnavailableError",
    "WakeAudioSample",
    "wake_tenant_key",
]
