from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_contracts():
    path = Path(__file__).with_name("audio_contracts.py")
    spec = importlib.util.spec_from_file_location("voice_oracle_audio_contracts_for_tests", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_contracts = _load_contracts()
assert_ducking_from_volume_samples = _contracts.assert_ducking_from_volume_samples
assert_silence_stt_recovery = _contracts.assert_silence_stt_recovery
assert_tts_audible_content = _contracts.assert_tts_audible_content


def test_rms_only_tts_without_transcript_terms_fails() -> None:
    result = assert_tts_audible_content(
        transcript="",
        expected_terms=["voice", "oracle"],
        rms=900.0,
        min_rms=50.0,
    )

    assert not result.ok
    assert result.details["rms_ok"]
    assert result.details["missing_terms"] == ["voice", "oracle"]


def test_tts_transcript_containing_expected_terms_passes() -> None:
    result = assert_tts_audible_content(
        transcript="Voice oracle check. Viola text to speech is active.",
        expected_terms=["voice", "speech"],
        rms=900.0,
        min_rms=50.0,
    )

    assert result.ok
    assert result.details["missing_terms"] == []


def test_empty_expected_tts_terms_fail() -> None:
    result = assert_tts_audible_content(
        transcript="Voice oracle check. Viola text to speech is active.",
        expected_terms=[],
        rms=900.0,
        min_rms=50.0,
    )

    assert not result.ok
    assert not result.details["expected_terms_present"]


def test_ducking_with_only_log_lines_no_samples_fails() -> None:
    result = assert_ducking_from_volume_samples(
        music_playing_at_baseline=True,
        baseline_volume=50,
        samples=[],
        min_attenuation_delta=10,
        restore_tolerance=4,
        log_lines=["DUCK_START: 50 -> 20", "Un-ducking audio: 20 -> 50"],
    )

    assert not result.ok
    assert "volume_samples_required" in result.details["failures"]


def test_ducking_with_attenuation_and_restore_passes() -> None:
    result = assert_ducking_from_volume_samples(
        music_playing_at_baseline=True,
        baseline_volume=50,
        samples=[
            {"volume": 50},
            {"volume": 34},
            {"volume": 38},
            {"volume": 49},
        ],
        min_attenuation_delta=10,
        restore_tolerance=3,
    )

    assert result.ok
    assert result.details["attenuation"]["volume"] == 34.0
    assert result.details["restore"]["volume"] == 49.0


def test_ducking_without_music_playing_fails() -> None:
    result = assert_ducking_from_volume_samples(
        music_playing_at_baseline=False,
        baseline_volume=50,
        samples=[50, 30, 50],
        min_attenuation_delta=10,
        restore_tolerance=3,
    )

    assert not result.ok
    assert "music_not_playing_at_baseline" in result.details["failures"]


def test_silence_recovery_fails_on_hallucinated_text_and_passes_on_empty_no_speech() -> None:
    hallucinated = assert_silence_stt_recovery(
        transcript="hello there",
        status_code=400,
        error_code="no_speech_detected",
        elapsed_ms=300.0,
        timeout_s=1.0,
    )
    empty_no_speech = assert_silence_stt_recovery(
        transcript="",
        status_code=400,
        error_code="no_speech_detected",
        elapsed_ms=300.0,
        timeout_s=1.0,
    )

    assert not hallucinated.ok
    assert not hallucinated.details["transcript_empty"]
    assert empty_no_speech.ok
    assert empty_no_speech.details["accepted_no_speech_error"]
