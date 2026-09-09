"""Pure audio evidence contracts for the live voice oracle."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AudioContractResult:
    ok: bool
    details: dict[str, Any]


def assert_transcribed_audio_content(
    *,
    transcript: str | None,
    expected_terms: Iterable[str] | None,
    rms: float,
    min_rms: float,
) -> AudioContractResult:
    terms = _normalized_terms(expected_terms)
    transcript_text = (transcript or "").strip()
    normalized_transcript = transcript_text.casefold()
    missing_terms = [term for term in terms if term not in normalized_transcript]
    rms_ok = float(rms) >= float(min_rms)
    terms_ok = bool(terms) and bool(transcript_text) and not missing_terms
    ok = rms_ok and terms_ok
    return AudioContractResult(
        ok=ok,
        details={
            "transcript": transcript or "",
            "expected_terms": terms,
            "missing_terms": missing_terms,
            "rms": float(rms),
            "min_rms": float(min_rms),
            "rms_ok": rms_ok,
            "terms_ok": terms_ok,
            "expected_terms_present": bool(terms),
        },
    )


def assert_tts_audible_content(
    *,
    transcript: str | None,
    expected_terms: Iterable[str] | None,
    rms: float,
    min_rms: float,
) -> AudioContractResult:
    result = assert_transcribed_audio_content(
        transcript=transcript,
        expected_terms=expected_terms,
        rms=rms,
        min_rms=min_rms,
    )
    return AudioContractResult(ok=result.ok, details={**result.details, "contract": "tts_audible_content"})


def assert_ducking_from_volume_samples(
    *,
    music_playing_at_baseline: bool,
    baseline_volume: int | float | str | None,
    samples: Sequence[Mapping[str, Any] | int | float | str],
    original_volume: int | float | str | None = None,
    min_attenuation_delta: float = 5.0,
    restore_tolerance: float = 3.0,
    log_lines: Sequence[str] | None = None,
) -> AudioContractResult:
    baseline = _as_float(baseline_volume)
    original = _as_float(original_volume)
    volumes = [_sample_volume(sample) for sample in samples]
    usable = [{"index": index, "volume": volume} for index, volume in enumerate(volumes) if volume is not None]
    failures: list[str] = []
    if not music_playing_at_baseline:
        failures.append("music_not_playing_at_baseline")
    if baseline is None:
        failures.append("baseline_volume_missing")
    if not samples:
        failures.append("volume_samples_required")
    if samples and not usable:
        failures.append("no_usable_volume_samples")
    if min_attenuation_delta <= 0:
        failures.append("min_attenuation_delta_must_be_positive")
    if restore_tolerance < 0:
        failures.append("restore_tolerance_must_not_be_negative")

    attenuation: dict[str, float | int] | None = None
    restore: dict[str, float | int | str] | None = None
    restore_targets: list[tuple[str, float]] = []
    if baseline is not None:
        restore_targets.append(("baseline", baseline))
    if original is not None and original != baseline:
        restore_targets.append(("original", original))
    if baseline is not None and min_attenuation_delta > 0:
        threshold = baseline - float(min_attenuation_delta)
        for item in usable:
            if float(item["volume"]) <= threshold:
                attenuation = {
                    "index": int(item["index"]),
                    "volume": float(item["volume"]),
                    "threshold": threshold,
                }
                break
    if attenuation is not None and restore_tolerance >= 0:
        for item in usable:
            if int(item["index"]) <= int(attenuation["index"]):
                continue
            for target_name, target_volume in restore_targets:
                delta = abs(float(item["volume"]) - target_volume)
                if delta <= float(restore_tolerance):
                    restore = {
                        "index": int(item["index"]),
                        "volume": float(item["volume"]),
                        "target": target_name,
                        "target_volume": target_volume,
                        "delta": delta,
                    }
                    break
            if restore is not None:
                break

    attenuated = attenuation is not None
    restored = restore is not None
    if baseline is not None and not attenuated:
        failures.append("attenuation_sample_missing")
    if attenuation is not None and not restored:
        failures.append("later_restore_sample_missing")

    ok = not failures
    return AudioContractResult(
        ok=ok,
        details={
            "music_playing_at_baseline": music_playing_at_baseline,
            "baseline_volume": baseline,
            "original_volume": original,
            "min_attenuation_delta": float(min_attenuation_delta),
            "restore_tolerance": float(restore_tolerance),
            "sample_count": len(samples),
            "usable_samples": usable,
            "attenuation": attenuation,
            "restore": restore,
            "attenuated": attenuated,
            "restored": restored,
            "log_lines": list(log_lines or []),
            "failures": failures,
        },
    )


def assert_silence_stt_recovery(
    *,
    transcript: str | None,
    status_code: int,
    error_code: str | None,
    elapsed_ms: float,
    timeout_s: float,
) -> AudioContractResult:
    transcript_text = (transcript or "").strip()
    within_timeout = float(elapsed_ms) <= float(timeout_s) * 1000.0
    no_speech_error = int(status_code) == 400 and str(error_code or "") in {
        "no_speech",
        "no_speech_detected",
    }
    status_ok = int(status_code) == 200 or no_speech_error
    transcript_empty = not transcript_text
    ok = transcript_empty and within_timeout and status_ok
    return AudioContractResult(
        ok=ok,
        details={
            "transcript": transcript or "",
            "transcript_empty": transcript_empty,
            "status_code": int(status_code),
            "error_code": error_code,
            "accepted_no_speech_error": no_speech_error,
            "elapsed_ms": float(elapsed_ms),
            "timeout_s": float(timeout_s),
            "within_timeout": within_timeout,
            "status_ok": status_ok,
        },
    )


def _normalized_terms(expected_terms: Iterable[str] | None) -> list[str]:
    if expected_terms is None:
        return []
    return [term for term in (str(raw).strip().casefold() for raw in expected_terms) if term]


def _sample_volume(sample: Mapping[str, Any] | int | float | str) -> float | None:
    if isinstance(sample, Mapping):
        for key in ("volume", "player_volume", "hub_local_volume", "level"):
            if key in sample:
                return _as_float(sample[key])
        return None
    return _as_float(sample)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "AudioContractResult",
    "assert_ducking_from_volume_samples",
    "assert_silence_stt_recovery",
    "assert_transcribed_audio_content",
    "assert_tts_audible_content",
]
