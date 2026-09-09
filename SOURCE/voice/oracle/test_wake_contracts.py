from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_WAKE_CONTRACTS_PATH = Path(__file__).with_name("wake_contracts.py")
_WAKE_CONTRACTS_SPEC = importlib.util.spec_from_file_location("wake_contracts", _WAKE_CONTRACTS_PATH)
assert _WAKE_CONTRACTS_SPEC is not None
assert _WAKE_CONTRACTS_SPEC.loader is not None
wake_contracts = importlib.util.module_from_spec(_WAKE_CONTRACTS_SPEC)
sys.modules[_WAKE_CONTRACTS_SPEC.name] = wake_contracts
_WAKE_CONTRACTS_SPEC.loader.exec_module(wake_contracts)

evaluate_negative_audio = wake_contracts.evaluate_negative_audio
evaluate_wake_policy = wake_contracts.evaluate_wake_policy


def test_all_layers_pass_with_no_failed_required_layer() -> None:
    text = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate', 'cooldown'] failed=[])\n"
        "Wake trigger approved: score=0.950, threshold=0.900"
    )

    result = evaluate_wake_policy(text)

    assert result.all_layers_passed
    assert result.failed_required_layers == ()


def test_required_layer_in_failed_layers_makes_all_layers_passed_false() -> None:
    text = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate', 'cooldown'] "
        "failed=['cooldown'])\n"
        "Wake trigger approved: score=0.950, threshold=0.900"
    )

    result = evaluate_wake_policy(text)

    assert not result.all_layers_passed
    assert result.failed_required_layers == ("cooldown",)


def test_missing_required_layer_fails() -> None:
    text = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate'] failed=[])\n"
        "Wake trigger approved: score=0.950, threshold=0.900"
    )

    result = evaluate_wake_policy(text)

    assert not result.all_layers_passed
    assert result.missing_required_layers == ("cooldown",)


def test_below_threshold_fails() -> None:
    text = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate', 'cooldown'] failed=[])\n"
        "Wake trigger approved: score=0.700, threshold=0.900"
    )

    result = evaluate_wake_policy(text)

    assert not result.all_layers_passed
    assert not result.threshold_requirement_passed


def test_negative_silence_with_diagnostics_and_no_accept_passes() -> None:
    text = "\n".join(
        (
            "WAKE_AUDIO_DIAG rms=0.00",
            "[STAGE1] audio_in frames=10 rms=0.00",
            "[STAGE2] score=0.001 threshold=0.900",
            "[STAGE5] policy: DENY reason=score",
        )
    )

    result = evaluate_negative_audio(text)

    assert result.passed
    assert result.inspected
    assert not result.accepted


def test_negative_with_listening_fails() -> None:
    text = "\n".join(
        (
            "WAKE_AUDIO_DIAG rms=0.00",
            "[STAGE2] score=0.001 threshold=0.900",
            "LISTENING",
        )
    )

    result = evaluate_negative_audio(text)

    assert not result.passed
    assert result.accepted
    assert "LISTENING" in result.accept_markers


def test_negative_with_no_diagnostics_fails() -> None:
    result = evaluate_negative_audio("silence stayed quiet")

    assert not result.passed
    assert not result.inspected
    assert result.failure_reasons == ("no_inspection_markers",)
