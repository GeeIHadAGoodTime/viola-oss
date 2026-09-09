from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

from voice.oracle.live_voice_oracle import (
    DEFAULT_AUDIBLE_LOOPBACK_RMS,
    DEFAULT_MIN_INJECTION_RMS,
    INTENT_PATTERNS,
    REQUIRED_LIVE_SETTINGS,
    TTS_PATTERNS,
    Oracle,
    StageResult,
    parse_args,
)


def _oracle(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    args = parse_args(["--root", str(root), "--report-dir", "_diag/2026-06-02/test"])
    oracle = Oracle(args)
    oracle.ctx.settings = {}
    return oracle


def test_skip_is_not_required_failure() -> None:
    oracle = object.__new__(Oracle)
    oracle.results = [StageResult("missing_loopback", "skip")]

    assert not oracle._has_required_failure()


def test_live_roundtrip_is_not_optional() -> None:
    source = inspect.getsource(Oracle.run)

    assert "live_virtual_mic_roundtrip" in source
    assert "if self.args.inject_live" not in source


def test_command_wav_generation_is_forbidden(tmp_path) -> None:
    oracle = _oracle(tmp_path)

    assert oracle._command_wav() is None
    assert oracle.results[-1].status == "fail"
    assert "real user command WAV is required" in oracle.results[-1].details["missing_capability"]


def test_auto_command_wav_is_explicit_guardrail_escape(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    args = parse_args(
        [
            "--root",
            str(root),
            "--report-dir",
            "_diag/2026-06-02/test",
            "--auto-command-wav",
        ]
    )
    oracle = Oracle(args)
    oracle.ctx.settings = {}

    result = oracle._environment_guardrails()

    assert result.status == "pass"
    assert result.details["failures"] == {}
    assert any("<auto-stimulus>" in item for item in result.evidence)


def test_guardrails_fail_for_deterministic_stt_and_force_wake(tmp_path, monkeypatch) -> None:
    oracle = _oracle(tmp_path)
    oracle.ctx.settings = {"force_wake": True}
    monkeypatch.setenv("VIOLA_TEST_TRANSCRIBER", "1")

    result = oracle._environment_guardrails()

    assert result.status == "fail"
    assert "deterministic_stt_env" in result.details["failures"]
    assert "force_wake_setting" in result.details["failures"]


def test_default_expected_transcript_terms_cover_full_command() -> None:
    args = parse_args([])

    assert args.expected_transcript_terms == ["volume", "up"]
    assert args.expected_tts_terms == ["volume", "up"]


def test_empty_expected_transcript_terms_fail_guardrails(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    command_wav = root / "real-command.wav"
    command_wav.write_bytes(b"RIFF")
    args = parse_args(
        [
            "--root",
            str(root),
            "--report-dir",
            "_diag/2026-06-02/test",
            "--command-wav",
            str(command_wav),
            "--expected-transcript-terms",
        ]
    )
    oracle = Oracle(args)

    result = oracle._environment_guardrails()

    assert result.status == "fail"
    assert result.details["failures"]["expected_transcript_terms"]["reason"] == "expected_terms_empty"


def test_wake_policy_snapshot_requires_all_five_layers_and_threshold_cross() -> None:
    good = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate', 'cooldown'] failed=[])\n"
        "Wake trigger approved: score=0.950, threshold=0.900"
    )
    missing_layer = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate'] failed=[])\n"
        "Wake trigger approved: score=0.950, threshold=0.900"
    )
    below_threshold = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate', 'cooldown'] failed=[])\n"
        "Wake trigger approved: score=0.700, threshold=0.900"
    )
    failed_layer = (
        "[STAGE5] policy: ALLOW reason=none "
        "(passed=['listening_gate', 'zero_input', 'primary_score', 'vad_gate', 'cooldown'] failed=['cooldown'])\n"
        "Wake trigger approved: score=0.950, threshold=0.900"
    )

    assert Oracle._wake_policy_snapshot(good)["all_layers_passed"]
    assert not Oracle._wake_policy_snapshot(missing_layer)["all_layers_passed"]
    assert not Oracle._wake_policy_snapshot(below_threshold)["all_layers_passed"]
    assert not Oracle._wake_policy_snapshot(failed_layer)["all_layers_passed"]


def test_report_preserves_complete_gap_evidence(tmp_path) -> None:
    oracle = _oracle(tmp_path)
    oracle.results = [
        StageResult(
            "gap_stage",
            "fail",
            details={"missing_capability": "loopback"},
            evidence=["evidence-%d" % idx for idx in range(8)],
        )
    ]

    oracle._write_reports()

    report = oracle.ctx.report_dir / "voice_oracle_latest.md"
    text = report.read_text(encoding="utf-8")
    assert "evidence-0" in text
    assert "evidence-7" in text
    assert "Skipped Surface Inventory" in text
    assert "loopback" in text


def test_stage_writes_incremental_report(tmp_path, monkeypatch) -> None:
    oracle = _oracle(tmp_path)
    writes = []
    monkeypatch.setattr(
        oracle,
        "_write_reports",
        lambda: writes.append([r.name for r in oracle.results]),
    )

    oracle._stage("one", lambda: StageResult("one", "pass"))

    assert writes == [["one"]]


def test_default_audible_loopback_threshold_is_meaningful() -> None:
    args = parse_args([])

    assert args.min_loopback_rms == DEFAULT_AUDIBLE_LOOPBACK_RMS
    assert args.min_loopback_rms >= 500.0


def test_acoustic_route_defaults_are_fail_closed() -> None:
    args = parse_args([])

    assert args.allow_acoustic_fallback is True
    assert args.min_injection_rms == DEFAULT_MIN_INJECTION_RMS
    assert args.command_injection_gain == 0.0
    assert args.ducking_play_query == "All The Small Things"
    assert args.ducking_play_source == "local"
    assert args.settings_apply_wait_s > 0
    assert args.no_restore_audio_route is False


def test_runtime_settings_switch_to_codex_spark_and_restore(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    settings_dir = root / ".viola"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"
    original = {
        **REQUIRED_LIVE_SETTINGS,
        "tts_enabled": False,
        "ai_source": "managed",
        "llm_model": "gpt-5.4-mini",
    }
    settings_path.write_text(json.dumps(original), encoding="utf-8")
    args = parse_args(["--root", str(root), "--report-dir", "_diag/2026-06-02/test"])
    oracle = Oracle(args)

    def fake_post(path, payload, timeout=None):
        assert path == "/v1/settings"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        data.update(payload["settings"])
        settings_path.write_text(json.dumps(data), encoding="utf-8")
        return 200, {"ok": True}

    monkeypatch.setattr(oracle, "_http_post_json", fake_post)

    result = oracle._runtime_settings()
    assert result.status == "pass"
    applied = json.loads(settings_path.read_text(encoding="utf-8"))
    assert applied["ai_source"] == "codex"
    assert applied["llm_model"] == "gpt-5.3-codex-spark"
    assert applied["tts_enabled"] is True

    restore = oracle._restore_runtime_settings()
    restored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert restore["status"] == "restored"
    assert restored["ai_source"] == "managed"
    assert restored["llm_model"] == "gpt-5.4-mini"
    assert restored["tts_enabled"] is False


def test_no_speech_error_code_counts_as_recovery_response() -> None:
    payload = {"ok": False, "error": {"code": "no_speech_detected"}}

    assert Oracle._extract_error_code(payload) == "no_speech_detected"


def test_audible_tts_fails_fast_when_tts_disabled(tmp_path) -> None:
    oracle = _oracle(tmp_path)
    oracle.ctx.settings = {"tts_enabled": False}

    result = oracle._tts_audible_loopback()

    assert result.status == "fail"
    assert "live voice turn output capture required" in result.details["missing_capability"]


def test_tts_proof_methods_do_not_instantiate_kokoro() -> None:
    assert "KokoroTTSEngine" not in inspect.getsource(Oracle._tts_real_pcm)
    assert "KokoroTTSEngine" not in inspect.getsource(Oracle._tts_audible_loopback)


def test_failure_and_model_load_strings_are_not_success_patterns() -> None:
    forbidden = {
        "OpenAI Responses request failed",
        "RateLimitError",
        "insufficient_quota",
        "Kokoro model loaded",
    }

    assert not forbidden.intersection(INTENT_PATTERNS)
    assert not forbidden.intersection(TTS_PATTERNS)


def test_direct_command_is_cross_lane_not_voice_proof() -> None:
    source = inspect.getsource(Oracle.run)

    assert "intent_live_command" not in source
    assert "core_loop_command_probe" in source


def test_core_loop_probe_classifies_200_failure_payload_as_cross_lane(tmp_path, monkeypatch) -> None:
    oracle = _oracle(tmp_path)

    def fake_post(path, payload, timeout=None):
        assert path == "/v1/command"
        return 200, {"ok": True, "data": {"message": "I couldn't complete that."}}

    monkeypatch.setattr(oracle, "_http_post_json", fake_post)

    result = oracle._core_loop_command_probe()

    assert result.status == "pass"
    assert result.details["voice_lane_proof"] is False
    assert result.details["classification"] == "cross_lane_core_loop_failure"


def test_audio_route_candidates_prefer_virtual_then_acoustic_fallback(tmp_path) -> None:
    oracle = _oracle(tmp_path)
    oracle.ctx.settings = {"output_device": "Speakers (Lenovo USB Audio)"}
    oracle.ctx.devices = [
        {
            "index": 3,
            "name": "MOTIV Mix Virtual Output (Shure",
            "in": 2,
            "out": 0,
            "sample_rate": 48000,
            "hostapi": 0,
        },
        {
            "index": 9,
            "name": "MOTIV Mix Virtual Input (Shure",
            "in": 0,
            "out": 2,
            "sample_rate": 48000,
            "hostapi": 0,
        },
        {
            "index": 1,
            "name": "Microphone (HD Pro Webcam C920)",
            "in": 2,
            "out": 0,
            "sample_rate": 48000,
            "hostapi": 0,
        },
        {
            "index": 10,
            "name": "Speakers (Lenovo USB Audio)",
            "in": 0,
            "out": 2,
            "sample_rate": 48000,
            "hostapi": 0,
        },
    ]

    routes = oracle._audio_route_candidates()

    assert routes[0]["kind"] == "virtual"
    assert routes[0]["listener_input"]["index"] == 3
    assert routes[0]["injection_output"]["index"] == 9
    acoustic = next(route for route in routes if route["kind"] == "acoustic_fallback")
    assert acoustic["listener_input"]["index"] == 1
    assert acoustic["injection_output"]["index"] == 10
    assert acoustic["runtime_output"] is None


def test_live_audio_route_skips_settings_when_acoustic_listener_already_active(tmp_path, monkeypatch) -> None:
    oracle = _oracle(tmp_path)
    log_path = tmp_path / "viola.log"
    log_path.write_text(
        "WAKE_AUDIO_DIAG: device=Microphone (HD Pro Webcam C920) (index=None)\n",
        encoding="utf-8",
    )
    oracle.ctx.settings = {
        "input_device": "",
        "output_device": "Amazonbasics210",
    }
    oracle.ctx.live_audio_route = {
        "kind": "acoustic_fallback",
        "listener_input": {
            "index": 1,
            "name": "Microphone (HD Pro Webcam C920)",
            "in": 2,
            "out": 0,
            "hostapi": 0,
        },
        "injection_output": {
            "index": 10,
            "name": "Speakers (Lenovo USB Audio)",
            "in": 0,
            "out": 2,
            "hostapi": 0,
        },
        "runtime_output": None,
        "reason": "speaker_to_microphone_automated_equivalent",
    }

    monkeypatch.setattr(oracle, "_latest_log_path", lambda: log_path)

    def fail_post(*_args, **_kwargs):
        raise AssertionError("settings POST should not run")

    monkeypatch.setattr(oracle, "_http_post_json", fail_post)

    result = oracle._live_audio_route()

    assert result.status == "pass"
    assert result.details["desired_settings"] == {}
    assert result.details["input_seen_before_settings"] is True
    assert result.details["response"]["skipped"] is True


def test_calibration_selects_acoustic_when_virtual_route_is_silent(tmp_path, monkeypatch) -> None:
    oracle = _oracle(tmp_path)
    oracle.args.calibration_gain = [8.0]
    oracle.args.max_calibration_attempts = 4
    wake_wav = tmp_path / "wake.wav"
    wake_wav.write_bytes(b"RIFF")
    virtual_route = {
        "kind": "virtual",
        "listener_input": {
            "index": 3,
            "name": "MOTIV Mix Virtual Output",
            "in": 2,
            "out": 0,
            "hostapi": 0,
        },
        "injection_output": {
            "index": 9,
            "name": "MOTIV Mix Virtual Input",
            "in": 0,
            "out": 2,
            "hostapi": 0,
        },
        "runtime_output": {
            "index": 10,
            "name": "Speakers (Lenovo USB Audio)",
            "in": 0,
            "out": 2,
            "hostapi": 0,
        },
        "reason": "requested_virtual_pair_same_hostapi",
    }
    acoustic_route = {
        "kind": "acoustic_fallback",
        "listener_input": {
            "index": 1,
            "name": "Microphone (HD Pro Webcam C920)",
            "in": 2,
            "out": 0,
            "hostapi": 0,
        },
        "injection_output": {
            "index": 10,
            "name": "Speakers (Lenovo USB Audio)",
            "in": 0,
            "out": 2,
            "hostapi": 0,
        },
        "runtime_output": {
            "index": 10,
            "name": "Speakers (Lenovo USB Audio)",
            "in": 0,
            "out": 2,
            "hostapi": 0,
        },
        "reason": "speaker_to_microphone_automated_equivalent",
    }
    oracle.ctx.audio_route_candidates = [virtual_route, acoustic_route]

    monkeypatch.setattr(oracle, "_ranked_wake_wavs", lambda: [wake_wav])
    monkeypatch.setattr(
        oracle,
        "_playrec_wav",
        lambda _wav, input_index, output_index, out_path, gain: {
            "path": str(out_path),
            "rms": 0.4 if output_index == 9 else 850.0,
            "peak": 1.0 if output_index == 9 else 1200.0,
            "duration_s": 1.0,
        },
    )
    monkeypatch.setattr(
        oracle,
        "_score_wake_audio",
        lambda path: 0.0 if "virtual" in str(path) else 0.98,
    )

    result = oracle._live_injection_route_calibration()

    assert result.status == "pass"
    assert oracle.ctx.live_audio_route["kind"] == "acoustic_fallback"
    assert oracle.ctx.selected_injection_gain == 8.0
    assert result.details["best_attempt"]["rms"] == 850.0
    assert [attempt["route_kind"] for attempt in result.details["attempts"]] == [
        "virtual",
        "acoustic_fallback",
    ]


def test_tts_loopback_capture_is_separated_from_command_injection() -> None:
    source = inspect.getsource(Oracle._live_virtual_mic_roundtrip)

    assert "capture_started_after_command" in source
    assert "_capture_loopback_window" in source
    assert "_capture_loopback_during" not in source
    assert "_start_player_sampler" not in source


def test_ducking_proof_samples_real_output_loopback() -> None:
    run_source = inspect.getsource(Oracle.run)
    duck_source = inspect.getsource(Oracle._music_ducking_real_output_level)

    assert "music_ducking_real_output_level" in run_source
    assert "_ducking_live_api" not in run_source
    assert "_capture_loopback_window" in duck_source
    assert "assert_ducking_from_volume_samples" in duck_source


def test_ducking_helper_starts_local_playback_when_needed(tmp_path, monkeypatch) -> None:
    oracle = _oracle(tmp_path)
    posts = []
    states = [
        {"ok": True, "data": {"is_playing": False, "volume": 30}},
        {
            "ok": True,
            "data": {
                "is_playing": True,
                "volume": 30,
                "position": 1,
                "now_playing": {"title": "All The Small Things"},
            },
        },
    ]

    def fake_get(path, timeout=None):
        assert path == "/v1/player/state"
        return 200, states.pop(0) if states else states[-1]

    def fake_post(path, payload, timeout=None):
        posts.append((path, payload))
        return 200, {"ok": True}

    monkeypatch.setattr(oracle, "_http_get_json", fake_get)
    monkeypatch.setattr(oracle, "_http_post_json", fake_post)

    result = oracle._ensure_music_playing_for_ducking(runtime_output={"name": "Speakers"})

    assert result["ok"] is True
    assert posts == [("/v1/play", {"query": "All The Small Things", "source": "local"})]
    assert result["polls"][-1]["is_playing"] is True
