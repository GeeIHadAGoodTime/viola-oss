from __future__ import annotations

from voice.oracle.proof_contracts import (
    evaluate_positive_log_lines,
    evaluate_trace_candidate,
    expected_command_terms,
)


def _trace_result(
    trace_text: str,
    *,
    command_text: str = "volume up",
    index_outcome: str | None = "success",
    candidate_mtime: float = 150.0,
    run_started_at: float = 100.0,
    run_finished_at: float = 200.0,
    supplied_terms: list[str] | None = None,
) -> dict:
    return evaluate_trace_candidate(
        command_text=command_text,
        trace_text=trace_text,
        index_outcome=index_outcome,
        candidate_mtime=candidate_mtime,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
        supplied_terms=supplied_terms,
    )


def test_quota_429_trace_is_rejected_even_when_command_seen() -> None:
    result = _trace_result('{"command_text":"volume up","outcome":"success","message":"429 insufficient_quota"}')

    assert not result["accepted"]
    assert result["reason"] == "provider_failure_seen"
    assert result["checks"]["command_seen"]
    assert "429" in result["provider_failure_markers"]
    assert "insufficient_quota" in result["provider_failure_markers"]


def test_unrelated_success_trace_is_rejected() -> None:
    result = _trace_result('{"command_text":"play jazz","outcome":"success","final":"done"}')

    assert not result["accepted"]
    assert result["reason"] == "command_not_seen"
    assert result["checks"]["outcome_success"]


def test_stale_trace_outside_run_window_is_rejected() -> None:
    result = _trace_result('{"command_text":"volume up","outcome":"success"}', candidate_mtime=99.9)

    assert not result["accepted"]
    assert result["reason"] == "candidate_mtime_outside_run_window"
    assert not result["checks"]["mtime_in_window"]


def test_success_trace_with_full_command_terms_and_no_provider_failure_is_accepted() -> None:
    result = _trace_result('{"user":"volume","assistant":"raised the level up","outcome":"success","error":null}')

    assert result["accepted"]
    assert result["reason"] == "accepted"
    assert result["checks"]["provider_failure_free"]
    assert result["all_terms_seen"]


def test_volume_alone_is_rejected_for_volume_up_command() -> None:
    terms = expected_command_terms("volume up", ["volume"])
    result = _trace_result('{"command_text":"volume up","outcome":"success"}', supplied_terms=["volume"])

    assert not terms["ok"]
    assert terms["reason"] == "expected_terms_partial"
    assert terms["missing_terms"] == ["up"]
    assert not result["accepted"]
    assert result["reason"] == "expected_terms_partial"


def test_failure_quota_and_model_load_tts_intent_lines_are_not_positive() -> None:
    result = evaluate_positive_log_lines(
        "\n".join(
            [
                "Pipeline AI result: insufficient_quota 429",
                "Agent loop starting: provider failure",
                "Speaking response: error from TTS engine",
                "Kokoro model loaded",
                "Pipeline AI result: intent=volume_up status=success",
            ]
        ),
        (
            "Pipeline AI result",
            "Agent loop starting:",
            "Speaking response:",
            "Kokoro model loaded",
        ),
    )

    assert result["accepted"]
    assert result["accepted_lines"] == ["Pipeline AI result: intent=volume_up status=success"]
    rejected = "\n".join(line["line"] for line in result["rejected_lines"])
    assert "insufficient_quota" in rejected
    assert "provider failure" in rejected
    assert "error from TTS engine" in rejected
    assert "Kokoro model loaded" in rejected
