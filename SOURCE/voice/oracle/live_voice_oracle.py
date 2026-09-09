"""Instrumented live oracle for the desktop voice lane.

The harness is intentionally not a unit test. It probes a running Viola
desktop instance, exercises real local voice components, and optionally pushes
audio through a virtual microphone pair to verify the live wake -> STT ->
intent -> TTS path.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import re
import sys
import threading
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    import httpx
except ImportError as exc:  # pragma: no cover - environment failure
    raise SystemExit("httpx is required to run the live voice oracle") from exc

from audio_core.portaudio_guard import PORTAUDIO_LOCK, open_stream, terminate_portaudio
from voice.oracle.audio_contracts import (
    assert_ducking_from_volume_samples,
    assert_silence_stt_recovery,
    assert_tts_audible_content,
)
from voice.oracle.proof_contracts import (
    evaluate_positive_log_lines,
    evaluate_trace_candidate,
    expected_command_terms,
    negative_evidence_markers,
)
from voice.oracle.runtime_identity import verify_runtime_identity
from voice.oracle.wake_contracts import evaluate_negative_audio, evaluate_wake_policy

DEFAULT_BASE_URL = "http://127.0.0.1:8756"
DEFAULT_COMMAND_TEXT = "volume up"
DEFAULT_TTS_TEXT = "Voice oracle check. Viola text to speech is active."
DEFAULT_DUCKING_PLAY_QUERY = "All The Small Things"
REQUIRED_LIVE_SETTINGS = {
    "voice_mode": "wake_word",
    "wake_word_engine": "violawake",
    "stt_engine": "whisper_local",
    "whisper_model": "tiny.en",
    "tts_enabled": True,
    "ai_source": "codex",
    "llm_model": "gpt-5.3-codex-spark",
}
DEFAULT_REPORT_DIR = Path("_diag") / "2026-06-02"
DEFAULT_AUDIBLE_LOOPBACK_RMS = 500.0
DEFAULT_MIN_INJECTION_RMS = 500.0
DEFAULT_INJECTION_GAINS = (4.0, 8.0, 16.0, 32.0)
DEFAULT_TRACE_READ_USER = "desktop-authenticated"
VIRTUAL_AUDIO_KEYWORDS = (
    "motiv mix",
    "virtual",
    "vb-cable",
    "cable input",
    "cable output",
    "voicemeeter",
)
NON_DEVICE_KEYWORDS = ("mapper", "primary sound")
LOG_CANDIDATES = (
    Path("logs") / "structured" / "viola-qt.log",
    Path("logs") / "viola-qt.log",
    Path("logs") / "viola.log",
)
WAKE_PATTERNS = (
    "Wake accepted; entering LISTENING",
    "VIOLA detected",
    "[WAKE->CMD]",
    "[WAKE",
)
STT_PATTERNS = (
    "Transcript received",
    "[PIPELINE] Transcription result",
    "[STT] Transcription complete",
)
TTS_PATTERNS = (
    "Speaking response:",
    "Kokoro synthesised",
)
DUCK_PATTERNS = (
    "DUCK_START",
    "Un-ducking audio",
    "DUCK_COMPLETE",
)
WAKE_DIAG_PATTERNS = (
    "WAKE_AUDIO_DIAG",
    "[DIAG_BUFFER]",
    "[STAGE2] score",
    "[SPOKE_DIAG]",
)
INTENT_PATTERNS = (
    "Pipeline processing",
    "Pipeline AI result",
    "AI routed:",
    "Agent loop starting:",
    "Agent task",
    "continue_listening",
)
VOICE_HANDLER_PATTERNS = (
    "handle_once() started",
    "Transcript received",
    "Speaking response:",
)
WHISPER_STT_PATTERNS = (
    "Whisper",
    "WhisperTranscriber",
    "whisper_local",
)
FORBIDDEN_STT_PATTERNS = (
    "DeterministicTranscriber",
    "VIOLA_TEST_TRANSCRIBER",
    "Deterministic transcription",
)
FORCE_WAKE_PATTERNS = (
    "FORCE_WAKE",
    "policy layers bypassed",
    "Bypassing policy DENY",
)
WAKE_POLICY_ALLOW_PATTERNS = (
    "[STAGE5] policy: ALLOW",
    "Wake trigger approved:",
)
WAKE_ACCEPT_PATTERNS = (
    "Wake trigger approved:",
    "VIOLA detected!",
)
REQUIRED_WAKE_POLICY_LAYERS = (
    "listening_gate",
    "zero_input",
    "primary_score",
    "vad_gate",
    "cooldown",
)
WAKE_FALSE_POSITIVE_PATTERNS = WAKE_POLICY_ALLOW_PATTERNS + WAKE_ACCEPT_PATTERNS


@dataclass
class StageResult:
    name: str
    status: str
    latency_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)


@dataclass
class OracleContext:
    root: Path
    report_dir: Path
    base_url: str
    timeout_s: float
    started_at: float = field(default_factory=time.time)
    settings: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    devices: list[dict[str, Any]] = field(default_factory=list)
    log_path: Path | None = None
    generated_command_wav: Path | None = None
    tts_pcm_path: Path | None = None
    tts_loopback_wav: Path | None = None
    live_roundtrip_tts_capture: dict[str, Any] = field(default_factory=dict)
    live_roundtrip_tts_transcript: str = ""
    ducking_volume_samples: list[dict[str, Any]] = field(default_factory=list)
    voice_side_effect: dict[str, Any] = field(default_factory=dict)
    cross_lane_command_probe: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trace_ids: list[str] = field(default_factory=list)
    live_roundtrip_log: str = ""
    command_trace_report: Path | None = None
    command_started_at: float | None = None
    command_finished_at: float | None = None
    wake_scores: list[dict[str, Any]] = field(default_factory=list)
    selected_wake_wav: Path | None = None
    selected_injection_gain: float | None = None
    live_audio_route: dict[str, Any] = field(default_factory=dict)
    headless_injection_limit: dict[str, Any] = field(default_factory=dict)
    audio_route_candidates: list[dict[str, Any]] = field(default_factory=list)
    audio_route_restore: dict[str, Any] = field(default_factory=dict)
    runtime_settings_restore: dict[str, Any] = field(default_factory=dict)
    command_stimulus: dict[str, Any] = field(default_factory=dict)
    ducking_output_level: dict[str, Any] = field(default_factory=dict)


class _StaticTraceKeyProvider:
    """One-shot key provider for an already-audited break-glass trace read.

    Mirrors ``tools/trace_grep.py``'s ``_StaticKeyProvider``: the caller
    unwraps the key once via ``KeyProvider.engineer_break_glass_unwrap()``
    (which writes the fail-closed ``action="break_glass"`` audit row) and
    hands the raw key to this wrapper so ``TraceReader`` never touches the
    routine (unaudited) ``KeyProvider`` unwrap path.
    """

    def __init__(self, trace_key: bytes) -> None:
        self.trace_key = trace_key

    def unwrap_trace_key(self) -> bytes:
        return self.trace_key

    def audit_decrypt(self, *, task_id: str, reason: str, actor: str) -> None:
        return None


class Oracle:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        root = Path(args.root).resolve()
        report_dir = (root / args.report_dir).resolve()
        report_dir.mkdir(parents=True, exist_ok=True)
        self.ctx = OracleContext(
            root=root,
            report_dir=report_dir,
            base_url=args.base_url.rstrip("/"),
            timeout_s=float(args.timeout),
        )
        self.results: list[StageResult] = []
        self.headers = self._headers()

    def run(self) -> int:
        try:
            self._stage("runtime_health", self._runtime_health)
            self._stage("runtime_settings", self._runtime_settings)
            self._stage("settings_truth", self._settings_truth)
            self._stage("environment_guardrails", self._environment_guardrails)
            self._stage("audio_devices", self._audio_devices)
            self._stage("wake_model_real_score", self._wake_model_real_score)
            self._stage("stt_live_transcribe", self._stt_live_transcribe)
            self._stage(
                "live_injection_route_calibration",
                self._live_injection_route_calibration,
            )
            self._stage("live_audio_route", self._live_audio_route)
            self._stage("log_wiring_truth", self._log_wiring_truth)
            self._stage("wake_negative_silence_live", self._wake_negative_silence_live)
            self._stage("wake_negative_confusable_live", self._wake_negative_confusable_live)
            self._stage("wake_negative_music_live", self._wake_negative_music_live)
            self._stage("live_virtual_mic_roundtrip", self._live_virtual_mic_roundtrip)
            self._stage("trace_reader_full_command_trace", self._trace_reader_full_command_trace)
            self._stage("tts_real_pcm", self._tts_real_pcm)
            self._stage("tts_audible_loopback", self._tts_audible_loopback)
            self._stage("music_ducking_real_output_level", self._music_ducking_real_output_level)
            self._stage("recovery_paths", self._recovery_paths)
            self._stage("core_loop_command_probe", self._core_loop_command_probe)
        finally:
            if not bool(self.args.no_restore_audio_route):
                self.ctx.audio_route_restore = self._restore_audio_route()
            self.ctx.runtime_settings_restore = self._restore_runtime_settings()
            self._safe_write_reports()
        return 1 if self._has_required_failure() else 0

    def _stage(self, name: str, fn: Callable[[], StageResult]) -> None:
        start = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:
            result = StageResult(name=name, status="fail", details={"error": repr(exc)})
        if result.latency_ms is None:
            result.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        if result.name != name:
            result.name = name
        self.results.append(result)
        self._safe_write_reports()

    def _safe_write_reports(self) -> None:
        try:
            self._write_reports()
        except Exception as exc:
            self.ctx.report_dir.mkdir(parents=True, exist_ok=True)
            error_path = self.ctx.report_dir / "voice_oracle_report_write_error.txt"
            error_path.write_text(repr(exc), encoding="utf-8")

    def _has_required_failure(self) -> bool:
        for result in self.results:
            if result.status not in ("pass", "skip"):
                return True
        return False

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = None
        for candidate in (
            self.args.api_key_file,
            Path(".viola") / "secrets" / "initial_api_key",
            Path("data") / "secrets" / "initial_api_key",
        ):
            if not candidate:
                continue
            path = Path(candidate)
            if not path.is_absolute():
                path = Path(self.args.root) / path
            if path.exists():
                key = path.read_text(encoding="utf-8").strip()
                break
        if key:
            headers["X-API-Key"] = key
        return headers

    def _http_get_json(self, path: str, timeout: float | None = None) -> tuple[int, dict[str, Any] | str]:
        url = f"{self.ctx.base_url}{path}"
        with httpx.Client(timeout=timeout or self.ctx.timeout_s) as client:
            response = client.get(url, headers=self.headers)
        try:
            return response.status_code, response.json()
        except Exception:
            return response.status_code, response.text[:1000]

    def _http_post_json(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any] | str]:
        url = f"{self.ctx.base_url}{path}"
        with httpx.Client(timeout=timeout or self.ctx.timeout_s) as client:
            response = client.post(url, json=payload, headers=self.headers)
        try:
            return response.status_code, response.json()
        except Exception:
            return response.status_code, response.text[:1000]

    def _runtime_health(self) -> StageResult:
        status_live, live = self._http_get_json("/health/live", timeout=3)
        status_health, health = self._http_get_json("/health", timeout=5)
        status_diag, diagnostics = self._http_get_json("/v1/diagnostics", timeout=10)
        if isinstance(diagnostics, dict):
            self.ctx.diagnostics = diagnostics
        runtime_cwd = ""
        if isinstance(diagnostics, dict):
            system_diag = diagnostics.get("system")
            if isinstance(system_diag, dict):
                runtime_cwd = str(system_diag.get("cwd") or "")
            if not runtime_cwd:
                found_cwd = self._find_nested_value(diagnostics, "cwd")
                runtime_cwd = str(found_cwd or "")
        cwd_ok = bool(runtime_cwd) and Path(runtime_cwd).resolve() == self.ctx.root
        identity = verify_runtime_identity(self.ctx.base_url, self.ctx.root)
        evidence = [
            f"/health/live status={status_live}",
            f"/health status={status_health}",
            f"/v1/diagnostics status={status_diag}",
            f"runtime_cwd={runtime_cwd or '<not reported>'}",
            f"listener_pid={identity.pid} identity_ok={identity.ok} reason={identity.reason or '<ok>'}",
        ]
        ok = status_live == 200 and status_health == 200 and status_diag == 200 and cwd_ok and identity.ok
        return StageResult(
            "runtime_health",
            "pass" if ok else "fail",
            details={
                "live": live,
                "health": health,
                "diagnostics_status": status_diag,
                "runtime_cwd": runtime_cwd,
                "expected_root": str(self.ctx.root),
                "cwd_ok": cwd_ok,
                "runtime_identity": asdict(identity),
            },
            evidence=evidence,
        )

    def _runtime_settings(self) -> StageResult:
        settings_path = self.ctx.root / ".viola" / "settings.json"
        if not settings_path.exists():
            return StageResult("runtime_settings", "fail", details={"missing": str(settings_path)})
        current = json.loads(settings_path.read_text(encoding="utf-8"))
        original = {key: current.get(key) for key in REQUIRED_LIVE_SETTINGS}
        desired = {key: value for key, value in REQUIRED_LIVE_SETTINGS.items() if current.get(key) != value}
        self.ctx.runtime_settings_restore = {
            "original": original,
            "desired": dict(REQUIRED_LIVE_SETTINGS),
            "changed": sorted(desired),
            "restored": False,
        }
        if not desired:
            self.ctx.settings = current
            return StageResult(
                "runtime_settings",
                "pass",
                details={**self.ctx.runtime_settings_restore, "status_code": None},
                evidence=["runtime settings already codex/gpt-5.3-codex-spark"],
            )

        status, payload = self._http_post_json("/v1/settings", {"settings": desired}, timeout=10)
        deadline = time.time() + float(self.args.settings_apply_wait_s)
        applied = False
        observed: dict[str, Any] = {}
        while time.time() < deadline:
            with contextlib.suppress(Exception):
                observed = json.loads(settings_path.read_text(encoding="utf-8"))
                if all(observed.get(key) == value for key, value in REQUIRED_LIVE_SETTINGS.items()):
                    applied = True
                    break
            time.sleep(0.25)
        if not observed:
            observed = {**current, **desired}
        self.ctx.settings = {**current, **desired}
        ok = status == 200 and applied
        return StageResult(
            "runtime_settings",
            "pass" if ok else "fail",
            details={
                **self.ctx.runtime_settings_restore,
                "status_code": status,
                "response": payload,
                "applied": applied,
                "observed": {key: observed.get(key) for key in REQUIRED_LIVE_SETTINGS},
            },
            evidence=[
                "settings_status=%s applied=%s ai_source=%s llm_model=%s"
                % (
                    status,
                    applied,
                    self.ctx.settings.get("ai_source"),
                    self.ctx.settings.get("llm_model"),
                )
            ],
        )

    def _restore_runtime_settings(self) -> dict[str, Any]:
        restore = dict(self.ctx.runtime_settings_restore or {})
        original = restore.get("original")
        changed = restore.get("changed")
        if not isinstance(original, dict) or not changed:
            return {"status": "skip", "reason": "no runtime settings changed"}
        payload_settings = {key: original.get(key) for key in changed}
        try:
            status, payload = self._http_post_json("/v1/settings", {"settings": payload_settings}, timeout=10)
            restore.update(
                {
                    "status": "restored" if status == 200 else "failed",
                    "status_code": status,
                    "response": payload,
                    "restored": status == 200,
                    "restored_settings": payload_settings,
                }
            )
            return restore
        except Exception as exc:
            restore.update({"status": "error", "error": repr(exc), "restored": False})
            return restore

    def _settings_truth(self) -> StageResult:
        settings_path = self.ctx.root / ".viola" / "settings.json"
        if not settings_path.exists():
            return StageResult("settings_truth", "fail", details={"missing": str(settings_path)})
        self.ctx.settings = json.loads(settings_path.read_text(encoding="utf-8"))
        expected = REQUIRED_LIVE_SETTINGS
        failures = {
            key: {"actual": self.ctx.settings.get(key), "expected": value}
            for key, value in expected.items()
            if self.ctx.settings.get(key) != value
        }
        model_path = self.ctx.root / "violawake_data" / "trained_models" / "temporal_cnn.onnx"
        tts_model = self.ctx.root / "models" / "tts" / "kokoro-v1.0.onnx"
        voices = self.ctx.root / "models" / "tts" / "voices-v1.0.bin"
        asset_failures = {
            "wake_model": str(model_path),
            "tts_model": str(tts_model),
            "tts_voices": str(voices),
        }
        missing_assets = {name: path for name, path in asset_failures.items() if not Path(path).exists()}
        details = {
            "settings_path": str(settings_path),
            "settings": {key: self.ctx.settings.get(key) for key in expected},
            "input_device": self.ctx.settings.get("input_device"),
            "output_device": self.ctx.settings.get("output_device"),
            "missing_assets": missing_assets,
            "failures": failures,
        }
        status = "pass" if not failures and not missing_assets else "fail"
        return StageResult("settings_truth", status, details=details)

    def _environment_guardrails(self) -> StageResult:
        env_values = {
            "VIOLA_TEST_TRANSCRIBER": os.environ.get("VIOLA_TEST_TRANSCRIBER"),
            "VIOLA_FORCE_WAKE": os.environ.get("VIOLA_FORCE_WAKE"),
        }
        settings_force_wake = bool(self.ctx.settings.get("force_wake", False))
        failures: dict[str, Any] = {}
        if env_values["VIOLA_TEST_TRANSCRIBER"]:
            failures["deterministic_stt_env"] = env_values["VIOLA_TEST_TRANSCRIBER"]
        if env_values["VIOLA_FORCE_WAKE"]:
            failures["force_wake_env"] = env_values["VIOLA_FORCE_WAKE"]
        if settings_force_wake:
            failures["force_wake_setting"] = settings_force_wake
        if not self.args.command_wav and not self.args.auto_command_wav:
            failures["real_command_wav_required"] = "provide --command-wav or use --auto-command-wav test stimulus"
        term_result = expected_command_terms(self.args.command_text, self.args.expected_transcript_terms)
        if not term_result["ok"]:
            failures["expected_transcript_terms"] = term_result
        evidence = [
            "VIOLA_TEST_TRANSCRIBER=%s" % ("set" if env_values["VIOLA_TEST_TRANSCRIBER"] else "unset"),
            "VIOLA_FORCE_WAKE=%s" % ("set" if env_values["VIOLA_FORCE_WAKE"] else "unset"),
            "settings.force_wake=%s" % settings_force_wake,
            "command_wav=%s"
            % (self.args.command_wav or ("<auto-stimulus>" if self.args.auto_command_wav else "<missing>")),
            "expected_transcript_terms=%s" % term_result["terms"],
        ]
        return StageResult(
            "environment_guardrails",
            "pass" if not failures else "fail",
            details={
                "forbidden_env": env_values,
                "settings_force_wake": settings_force_wake,
                "expected_terms": term_result,
                "failures": failures,
            },
            evidence=evidence,
        )

    def _audio_devices(self) -> StageResult:
        try:
            import sounddevice as sd
        except ImportError:
            return StageResult("audio_devices", "fail", details={"error": "sounddevice unavailable"})

        from audio_core.portaudio_guard import sounddevice_guard

        with sounddevice_guard():
            _device_snapshot = list(sd.query_devices())

        devices: list[dict[str, Any]] = []
        for index, raw in enumerate(_device_snapshot):
            if not isinstance(raw, dict):
                continue
            devices.append(
                {
                    "index": index,
                    "name": str(raw.get("name", "")),
                    "in": int(raw.get("max_input_channels", 0)),
                    "out": int(raw.get("max_output_channels", 0)),
                    "sample_rate": float(raw.get("default_samplerate", 0) or 0),
                    "hostapi": int(raw.get("hostapi", -1)),
                }
            )
        self.ctx.devices = devices
        self.ctx.audio_route_candidates = self._audio_route_candidates()
        input_devices = [d for d in devices if d["in"] > 0]
        output_devices = [d for d in devices if d["out"] > 0]
        virtual_output = self._find_device(self.args.virtual_output, output=True)
        virtual_input = self._find_device(self.args.virtual_input, input=True)
        details = {
            "input_count": len(input_devices),
            "output_count": len(output_devices),
            "configured_input": self.ctx.settings.get("input_device"),
            "configured_output": self.ctx.settings.get("output_device"),
            "virtual_output": virtual_output,
            "virtual_input": virtual_input,
            "route_candidates": self.ctx.audio_route_candidates,
        }
        ok = bool(input_devices) and bool(output_devices) and bool(self.ctx.audio_route_candidates)
        return StageResult("audio_devices", "pass" if ok else "fail", details=details)

    def _audio_route_candidates(self) -> list[dict[str, Any]]:
        routes: list[dict[str, Any]] = []
        virtual_inputs = self._matching_devices(self.args.virtual_input, input=True)
        virtual_outputs = self._matching_devices(self.args.virtual_output, output=True)
        for output in virtual_outputs:
            for input_device in virtual_inputs:
                if output.get("hostapi") != input_device.get("hostapi"):
                    continue
                routes.append(
                    self._route_candidate(
                        kind="virtual",
                        listener_input=input_device,
                        injection_output=output,
                        runtime_output=self._preferred_runtime_output(exclude=output),
                        reason="requested_virtual_pair_same_hostapi",
                    )
                )
        if virtual_outputs and virtual_inputs and not routes:
            routes.append(
                self._route_candidate(
                    kind="virtual",
                    listener_input=virtual_inputs[0],
                    injection_output=virtual_outputs[0],
                    runtime_output=self._preferred_runtime_output(exclude=virtual_outputs[0]),
                    reason="requested_virtual_pair_cross_hostapi",
                )
            )

        if bool(self.args.allow_acoustic_fallback):
            physical_inputs = [
                device
                for device in self.ctx.devices
                if device["in"] > 0 and not self._device_is_virtual(device) and self._is_specific_device(device)
            ]
            physical_outputs = [
                device
                for device in self.ctx.devices
                if device["out"] > 0 and not self._device_is_virtual(device) and self._is_specific_device(device)
            ]
            physical_inputs.sort(key=self._acoustic_input_rank, reverse=True)
            physical_outputs.sort(key=self._acoustic_output_rank, reverse=True)
            max_routes = max(0, int(self.args.max_acoustic_routes))
            for input_device in physical_inputs[:2]:
                for output in physical_outputs[:max_routes]:
                    if output.get("hostapi") != input_device.get("hostapi"):
                        continue
                    routes.append(
                        self._route_candidate(
                            kind="acoustic_fallback",
                            listener_input=input_device,
                            injection_output=output,
                            runtime_output=None,
                            reason="speaker_to_microphone_automated_equivalent",
                        )
                    )
        return routes

    def _route_candidate(
        self,
        *,
        kind: str,
        listener_input: dict[str, Any],
        injection_output: dict[str, Any],
        runtime_output: dict[str, Any] | None,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "listener_input": dict(listener_input),
            "injection_output": dict(injection_output),
            "runtime_output": dict(runtime_output) if runtime_output else None,
            "reason": reason,
        }

    def _preferred_runtime_output(self, *, exclude: dict[str, Any] | None = None) -> dict[str, Any] | None:
        configured = str(self.ctx.settings.get("output_device", "") or "")
        configured_device = self._find_device(configured, output=True) if configured else None
        if configured_device and not self._same_device(configured_device, exclude):
            return configured_device
        physical_outputs = [
            device
            for device in self.ctx.devices
            if device["out"] > 0
            and not self._device_is_virtual(device)
            and self._is_specific_device(device)
            and not self._same_device(device, exclude)
        ]
        physical_outputs.sort(key=self._acoustic_output_rank, reverse=True)
        return physical_outputs[0] if physical_outputs else None

    def _runtime_capture_output(self, route: dict[str, Any] | None) -> dict[str, Any] | None:
        return (
            (route or {}).get("runtime_output")
            or self._preferred_runtime_output()
            or (route or {}).get("injection_output")
        )

    def _matching_devices(
        self, query: str | None, *, input: bool = False, output: bool = False
    ) -> list[dict[str, Any]]:
        if not query:
            return []
        lower = query.lower()
        matches = []
        for device in self.ctx.devices:
            if input and device["in"] <= 0:
                continue
            if output and device["out"] <= 0:
                continue
            name = str(device["name"]).lower()
            if lower in name or name in lower:
                matches.append(device)
        matches.sort(
            key=lambda item: (
                0 if self._device_is_virtual(item) else 1,
                int(item.get("hostapi", 99)),
            )
        )
        return matches

    @staticmethod
    def _same_device(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
        if left is None or right is None:
            return False
        return int(left.get("index", -1)) == int(right.get("index", -2))

    @staticmethod
    def _device_is_virtual(device: dict[str, Any]) -> bool:
        name = str(device.get("name", "")).lower()
        return any(keyword in name for keyword in VIRTUAL_AUDIO_KEYWORDS)

    @staticmethod
    def _is_specific_device(device: dict[str, Any]) -> bool:
        name = str(device.get("name", "")).lower()
        return bool(name.strip()) and not any(keyword in name for keyword in NON_DEVICE_KEYWORDS)

    @staticmethod
    def _acoustic_input_rank(device: dict[str, Any]) -> tuple[int, int]:
        name = str(device.get("name", "")).lower()
        score = 0
        if "c920" in name or "webcam" in name:
            score += 50
        if "microphone" in name:
            score += 25
        if "lenovo" in name:
            score += 10
        return score, -int(device.get("hostapi", 99))

    @staticmethod
    def _acoustic_output_rank(device: dict[str, Any]) -> tuple[int, int]:
        name = str(device.get("name", "")).lower()
        score = 0
        if "speakers" in name:
            score += 70
        if "lenovo" in name:
            score += 50
        if "high definition" in name:
            score += 35
        if "headphones" in name:
            score += 20
        if "amazonbasics" in name:
            score -= 10
        return score, -int(device.get("hostapi", 99))

    def _log_wiring_truth(self) -> StageResult:
        log_path = self._latest_log_path()
        self.ctx.log_path = log_path
        if log_path is None:
            return StageResult("log_wiring_truth", "fail", details={"error": "no Viola log found"})
        text = ""
        deadline = time.time() + float(self.args.log_fresh_wait_s)
        while time.time() < deadline:
            text = self._log_text_since_start(log_path, max_chars=2_000_000)
            if "WAKE_AUDIO_DIAG" in text or "[STAGE1] audio_in" in text:
                break
            time.sleep(0.5)
        checks = {
            "fresh_log_after_oracle_start": bool(text.strip()),
            "audio_frames": "[STAGE1] audio_in" in text or "WAKE_AUDIO_DIAG" in text,
            "no_force_wake": not any(pattern in text for pattern in FORCE_WAKE_PATTERNS),
            "no_deterministic_stt": not any(pattern in text for pattern in FORBIDDEN_STT_PATTERNS),
        }
        device_lines = [
            line.strip() for line in text.splitlines() if "AUDIO_DEVICE:" in line or "WAKE_AUDIO_DIAG" in line
        ][-12:]
        status = "pass" if all(checks.values()) else "fail"
        return StageResult(
            "log_wiring_truth",
            status,
            details={
                "log_path": str(log_path),
                "checks": checks,
                "device_lines": device_lines,
                "started_at": self.ctx.started_at,
            },
            evidence=device_lines,
        )

    def _wake_model_real_score(self) -> StageResult:
        model = self.ctx.root / "violawake_data" / "trained_models" / "temporal_cnn.onnx"
        if not model.exists():
            return StageResult("wake_model_real_score", "fail", details={"missing_model": str(model)})
        wake_wavs = self._wake_wavs()
        if not wake_wavs:
            return StageResult(
                "wake_model_real_score",
                "fail",
                details={
                    "error": "no wake wav supplied or found",
                    "wake_wav": self.args.wake_wav,
                },
            )
        try:
            from violawake import ViolaWake
        except ImportError as exc:
            return StageResult("wake_model_real_score", "fail", details={"error": repr(exc)})

        engine = ViolaWake(
            model_path=str(model),
            threshold=float(self.args.wake_threshold),
            debounce_seconds=0.0,
        )
        scores = []
        for wav_path in wake_wavs[: self.args.max_wake_scan]:
            audio = self._read_wav_float32(wav_path, target_rate=16_000)
            score = float(engine.process_audio(audio))
            scores.append({"path": str(wav_path), "score": score})
        best = max(scores, key=lambda item: item["score"])
        self.ctx.wake_scores = scores
        self.ctx.selected_wake_wav = Path(str(best["path"]))
        status = "pass" if best["score"] >= float(self.args.wake_threshold) else "fail"
        return StageResult(
            "wake_model_real_score",
            status,
            details={
                "threshold": self.args.wake_threshold,
                "best": best,
                "scores": scores,
            },
            evidence=[f"{best['path']} score={best['score']:.3f}"],
        )

    def _wake_negative_silence_live(self) -> StageResult:
        silence = self._silence_wav(duration_s=float(self.args.negative_wait_s))
        return self._play_negative_and_assert_no_wake(
            stage="wake_negative_silence_live",
            wav_path=silence,
            reason="silence must not trigger wake",
        )

    def _wake_negative_confusable_live(self) -> StageResult:
        negatives = self._negative_wavs()
        if not negatives:
            return StageResult(
                "wake_negative_confusable_live",
                "skip",
                details={
                    "missing_capability": "no confusable negative WAV supplied or found",
                    "negative_wav": self.args.negative_wav,
                },
            )
        return self._play_negative_and_assert_no_wake(
            stage="wake_negative_confusable_live",
            wav_path=negatives[0],
            reason="confusable speech must not trigger wake",
        )

    def _wake_negative_music_live(self) -> StageResult:
        music_wavs = self._music_negative_wavs()
        if not music_wavs:
            return StageResult(
                "wake_negative_music_live",
                "skip",
                details={
                    "missing_capability": "no music negative WAV supplied or found",
                    "music_negative_wav": self.args.music_negative_wav,
                },
            )
        return self._play_negative_and_assert_no_wake(
            stage="wake_negative_music_live",
            wav_path=music_wavs[0],
            reason="music playback audio must not false-trigger wake",
        )

    def _play_negative_and_assert_no_wake(self, *, stage: str, wav_path: Path, reason: str) -> StageResult:
        route = self.ctx.live_audio_route
        injection_output = (
            route.get("injection_output") if route else self._find_device(self.args.virtual_output, output=True)
        )
        listener_input = (
            route.get("listener_input") if route else self._find_device(self.args.virtual_input, input=True)
        )
        log_path = self._latest_log_path()
        if injection_output is None or listener_input is None or log_path is None:
            return StageResult(
                stage,
                "skip",
                details={
                    "missing_capability": "live input/output/log path required",
                    "injection_output": injection_output,
                    "listener_input": listener_input,
                    "log_path": str(log_path) if log_path else None,
                },
            )
        if not wav_path.exists():
            return StageResult(
                stage,
                "skip",
                details={
                    "missing_capability": "negative WAV path does not exist",
                    "missing_wav": str(wav_path),
                },
            )
        offset = log_path.stat().st_size
        start = time.perf_counter()
        try:
            self._play_wav(
                wav_path,
                int(injection_output["index"]),
                gain=float(self.args.negative_injection_gain),
            )
        except Exception as exc:
            return StageResult(stage, "fail", details={"error": repr(exc), "wav": str(wav_path)})
        wait_s = float(self.args.negative_wait_s)
        time.sleep(max(0.5, wait_s))
        text = self._read_from_offset(log_path, offset)
        matches = self._matching_lines(
            text,
            WAKE_FALSE_POSITIVE_PATTERNS + WAKE_DIAG_PATTERNS + FORCE_WAKE_PATTERNS,
        )
        negative_eval = evaluate_negative_audio(text).to_dict()
        false_wake = bool(negative_eval["accepted"])
        force_wake = any(pattern in text for pattern in FORCE_WAKE_PATTERNS)
        passed = bool(negative_eval["passed"]) and not force_wake
        return StageResult(
            stage,
            "pass" if passed else "fail",
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            details={
                "wav": str(wav_path),
                "reason": reason,
                "route_kind": route.get("kind") if route else "direct_args",
                "injection_output": injection_output,
                "listener_input": listener_input,
                "false_wake": false_wake,
                "force_wake": force_wake,
                "negative_audio_evaluation": negative_eval,
                "wait_s": wait_s,
                "log_excerpt": matches,
            },
            evidence=matches,
        )

    def _stt_live_transcribe(self) -> StageResult:
        wav_path = self._command_wav()
        if wav_path is None or not wav_path.exists():
            return StageResult(
                "stt_live_transcribe",
                "fail",
                details={"error": "command wav unavailable"},
            )
        log_path = self._latest_log_path()
        offset = log_path.stat().st_size if log_path and log_path.exists() else 0
        start = time.perf_counter()
        url = f"{self.ctx.base_url}/v1/transcribe"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        with httpx.Client(timeout=max(45.0, self.ctx.timeout_s)) as client:
            with wav_path.open("rb") as fh:
                response = client.post(
                    url,
                    headers=headers,
                    files={"audio": (wav_path.name, fh, "audio/wav")},
                )
        latency = round((time.perf_counter() - start) * 1000, 1)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:1000]}
        transcript = self._extract_transcript(payload)
        term_result = expected_command_terms(self.args.command_text, self.args.expected_transcript_terms)
        expected_terms = list(term_result["terms"])
        normalized = transcript.lower()
        terms_ok = bool(term_result["ok"]) and all(term in normalized for term in expected_terms)
        new_text = self._read_from_offset(log_path, offset) if log_path else ""
        forbidden = self._matching_lines(new_text, FORBIDDEN_STT_PATTERNS)
        whisper_lines = self._matching_lines(new_text, WHISPER_STT_PATTERNS)
        ok = (
            response.status_code == 200
            and bool(transcript.strip())
            and terms_ok
            and bool(whisper_lines)
            and not forbidden
        )
        return StageResult(
            "stt_live_transcribe",
            "pass" if ok else "fail",
            latency_ms=latency,
            details={
                "status_code": response.status_code,
                "wav": str(wav_path),
                "transcript": transcript,
                "expected_terms": expected_terms,
                "expected_terms_contract": term_result,
                "response": payload,
                "whisper_evidence": whisper_lines,
                "forbidden_stt_evidence": forbidden,
            },
            evidence=[f"transcript={transcript!r}", f"wav={wav_path}"] + whisper_lines + forbidden,
        )

    def _core_loop_command_probe(self) -> StageResult:
        """Observe /v1/command health without using it as voice-lane proof."""
        start = time.perf_counter()
        try:
            status, payload = self._http_post_json(
                "/v1/command",
                {
                    "text": self.args.command_text,
                    "origin_channel": "voice_oracle_cross_lane_probe",
                },
                timeout=float(self.args.intent_timeout_s),
            )
        except Exception as exc:
            details = {
                "classification": "cross_lane_core_loop_unreachable",
                "error": repr(exc),
                "voice_lane_proof": False,
            }
            self.ctx.cross_lane_command_probe = details
            return StageResult(
                "core_loop_command_probe",
                "pass",
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                details=details,
                evidence=["/v1/command probe unreachable; not counted as voice proof"],
            )
        payload_text = (
            json.dumps(payload, default=str, ensure_ascii=False) if isinstance(payload, dict) else str(payload)
        )
        markers = negative_evidence_markers(payload_text)
        classification = "cross_lane_command_observed"
        if status == 429 or any(marker in markers for marker in ("429", "insufficient_quota", "quota", "rate limit")):
            classification = "cross_lane_core_loop_429"
        elif markers:
            classification = "cross_lane_core_loop_failure"
        elif status != 200:
            classification = "cross_lane_core_loop_non_200"
        details = {
            "classification": classification,
            "status_code": status,
            "response": payload,
            "negative_markers": markers,
            "voice_lane_proof": False,
        }
        self.ctx.cross_lane_command_probe = details
        return StageResult(
            "core_loop_command_probe",
            "pass",
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            details=details,
            evidence=[f"/v1/command status={status} classification={classification}"],
        )

    def _trace_reader_full_command_trace(self) -> StageResult:
        after = self.ctx.command_started_at or self.ctx.started_at
        before = (self.ctx.command_finished_at or time.time()) + 5.0
        candidates = self._trace_files_between(after, before)
        if not candidates:
            if self.ctx.headless_injection_limit and self.ctx.command_started_at is None:
                return StageResult(
                    "trace_reader_full_command_trace",
                    "skip",
                    details={
                        "missing_capability": "live voice command trace depends on full live-mic RMS roundtrip",
                        "proof_source": "live_virtual_mic_roundtrip",
                        "headless_injection_limit": self.ctx.headless_injection_limit,
                        "searched_roots": [str(root) for root in self._trace_root_candidates()],
                    },
                    evidence=[
                        "trace skipped: live-mic RMS roundtrip documented headless harness limit",
                    ],
                )
            return StageResult(
                "trace_reader_full_command_trace",
                "fail",
                details={
                    "missing_capability": "no task trace file created during command window",
                    "command_started_at": self.ctx.command_started_at,
                    "command_finished_at": self.ctx.command_finished_at,
                    "searched_roots": [str(root) for root in self._trace_root_candidates()],
                },
            )

        try:
            from core.user_context import get_desktop_authenticated_user_id
            from intent.task_trace_reader import TraceReader
            from services.persistence.trace_keys import KeyProvider
        except ImportError as exc:
            return StageResult("trace_reader_full_command_trace", "fail", details={"error": repr(exc)})

        user_id = str(self.args.trace_user_id or "")
        if not user_id or user_id == DEFAULT_TRACE_READ_USER:
            try:
                user_id = get_desktop_authenticated_user_id()
            except LookupError as exc:
                return StageResult(
                    "trace_reader_full_command_trace",
                    "fail",
                    details={
                        "error": "logged-in desktop user_id is required for trace reading",
                        "exception": repr(exc),
                    },
                )
        key_provider = KeyProvider(user_id)
        readable: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for path in candidates:
            task_id = path.name.split(".trace", 1)[0]
            try:
                # Engineer content read: route through the audited break-glass
                # path so a fail-closed action="break_glass" row is written
                # for this decrypt, same contract as tools/trace_grep.py's
                # _StaticKeyProvider (this oracle reads a fixed set of already
                # -unwrapped trace files rather than lazily unwrapping one, so
                # the one-shot static provider fits better than the lazy
                # variant used by the oracle files under tools/oracles/).
                trace_key = key_provider.engineer_break_glass_unwrap(
                    task_id=task_id,
                    reason="live_voice_oracle command trace verification",
                    actor="voice-oracle",
                )
                content_key_provider = _StaticTraceKeyProvider(trace_key)
                reader = TraceReader(
                    task_id=task_id,
                    user_id=user_id,
                    key_provider=content_key_provider,
                    root_dir=self.ctx.root,
                    recover_corrupt_tail=True,
                    audit_actor="voice-oracle",
                )
                index = reader.index()
                events = [event.payload for event in reader.stream()]
                steps = [asdict(reader.step(i + 1)) for i in range(index.total_steps)]
                trace_text = json.dumps({"events": events, "steps": steps}, default=str, ensure_ascii=False)
                evaluation = evaluate_trace_candidate(
                    command_text=self.args.command_text,
                    trace_text=trace_text,
                    index_outcome=index.outcome,
                    candidate_mtime=path.stat().st_mtime,
                    run_started_at=after,
                    run_finished_at=before,
                    supplied_terms=self.args.expected_transcript_terms,
                    candidate_path=path,
                )
                report_path = self.ctx.report_dir / ("trace_reader_%s.json" % task_id)
                report_path.write_text(
                    json.dumps(
                        {
                            "task_id": task_id,
                            "path": str(reader.path),
                            "index": asdict(index),
                            "evaluation": evaluation,
                            "events": events,
                            "steps": steps,
                        },
                        indent=2,
                        default=str,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                readable.append(
                    {
                        "task_id": task_id,
                        "path": str(reader.path),
                        "report_path": str(report_path),
                        "event_counts": index.event_counts,
                        "total_steps": index.total_steps,
                        "outcome": index.outcome,
                        "accepted": evaluation["accepted"],
                        "reason": evaluation["reason"],
                        "checks": evaluation["checks"],
                        "command_seen": evaluation["checks"]["command_seen"],
                        "provider_failure_seen": evaluation["provider_failure_seen"],
                        "provider_failure_markers": evaluation["provider_failure_markers"],
                    }
                )
                if evaluation["accepted"]:
                    self.ctx.trace_ids.append(task_id)
                    self.ctx.command_trace_report = report_path
            except Exception as exc:
                failures.append({"path": str(path), "error": repr(exc)})
        ok = bool(readable) and any(item.get("accepted") for item in readable)
        return StageResult(
            "trace_reader_full_command_trace",
            "pass" if ok else "fail",
            details={
                "readable": readable,
                "failures": failures,
                "candidate_paths": [str(path) for path in candidates],
                "user_id": user_id,
            },
            evidence=[
                "trace_id=%s outcome=%s accepted=%s reason=%s command_seen=%s provider_failure_seen=%s"
                % (
                    item["task_id"],
                    item["outcome"],
                    item["accepted"],
                    item["reason"],
                    item["command_seen"],
                    item["provider_failure_seen"],
                )
                for item in readable
            ],
            trace_ids=[str(item["task_id"]) for item in readable if item.get("accepted")],
        )

    def _tts_real_pcm(self) -> StageResult:
        capture = self.ctx.live_roundtrip_tts_capture
        if not capture:
            return StageResult(
                "tts_real_pcm",
                "fail",
                details={
                    "missing_capability": "running voice turn loopback capture required",
                    "proof_source": "live_virtual_mic_roundtrip",
                },
            )
        if capture.get("status") == "skip":
            return StageResult(
                "tts_real_pcm",
                "skip",
                details={
                    **capture,
                    "missing_capability": "TTS PCM proof depends on full live-mic RMS roundtrip",
                    "proof_source": "live_virtual_mic_roundtrip",
                },
                evidence=[f"capture_status={capture.get('status')}"],
            )
        rms = float(capture.get("rms") or 0.0)
        ok = (
            capture.get("status") == "captured"
            and rms >= float(self.args.min_loopback_rms)
            and not capture.get("fn_errors")
        )
        return StageResult(
            "tts_real_pcm",
            "pass" if ok else "fail",
            details={
                **capture,
                "rms": rms,
                "min_rms": self.args.min_loopback_rms,
                "proof_source": "running_voice_stack_loopback",
            },
            evidence=[f"capture={capture.get('path')}", f"rms={rms:.1f}"],
        )

    def _tts_audible_loopback(self) -> StageResult:
        capture = self.ctx.live_roundtrip_tts_capture
        if not capture:
            return StageResult(
                "tts_audible_loopback",
                "fail",
                details={"missing_capability": "live voice turn output capture required"},
            )
        transcript = self.ctx.live_roundtrip_tts_transcript
        contract = assert_tts_audible_content(
            transcript=transcript,
            expected_terms=self.args.expected_tts_terms,
            rms=float(capture.get("rms") or 0.0),
            min_rms=float(self.args.min_loopback_rms),
        )
        if capture.get("status") == "skip":
            return StageResult(
                "tts_audible_loopback",
                "skip",
                details={
                    "missing_capability": "physical loopback capture from running voice stack required",
                    "capture": capture,
                    "content_contract": contract.details,
                },
                evidence=[f"capture_status={capture.get('status')}"],
            )
        ok = bool(contract.ok) and not capture.get("fn_errors")
        return StageResult(
            "tts_audible_loopback",
            "pass" if ok else "fail",
            details={
                **capture,
                "transcript": transcript,
                "expected_terms": self.args.expected_tts_terms,
                "content_contract": contract.details,
                "min_loopback_rms": self.args.min_loopback_rms,
            },
            evidence=[
                f"capture={capture.get('path')}",
                f"rms={capture.get('rms', 0):.4f}",
                f"tts_transcript={transcript!r}",
            ],
        )

    def _ducking_live_api(self) -> StageResult:
        if not self.headers.get("X-API-Key"):
            return StageResult("ducking_live_api", "fail", details={"error": "no API key available"})
        log_path = self._latest_log_path()
        offset = log_path.stat().st_size if log_path and log_path.exists() else 0
        duck_status, duck_payload = self._http_post_json("/v1/audio/duck", {}, timeout=5)
        time.sleep(0.7)
        unduck_status, unduck_payload = self._http_post_json("/v1/audio/unduck", {}, timeout=5)
        time.sleep(0.7)
        new_text = self._read_from_offset(log_path, offset) if log_path else ""
        checks = {
            "duck_api_200": duck_status == 200,
            "unduck_api_200": unduck_status == 200,
            "duck_start_log": "DUCK_START" in new_text,
            "restore_log": "Un-ducking audio" in new_text or "DUCK_COMPLETE" in new_text,
        }
        playback_state = (self.ctx.diagnostics or {}).get("playback_state")
        status = "pass" if all(checks.values()) else "fail"
        return StageResult(
            "ducking_live_api",
            status,
            details={
                "checks": checks,
                "duck_response": duck_payload,
                "unduck_response": unduck_payload,
                "playback_state": playback_state,
                "log_excerpt": self._matching_lines(new_text, DUCK_PATTERNS)[-12:],
            },
            evidence=self._matching_lines(new_text, DUCK_PATTERNS)[-6:],
        )

    def _music_ducking_real_output_level(self) -> StageResult:
        route = self.ctx.live_audio_route
        runtime_output = self._runtime_capture_output(route)
        playback_start = self._ensure_music_playing_for_ducking(runtime_output=runtime_output)
        if not playback_start.get("ok"):
            return StageResult(
                "music_ducking_real_output_level",
                "fail",
                details={
                    "missing_capability": "active music playback is required for sampled output ducking proof",
                    "playback_start": playback_start,
                },
            )
        player_state = playback_start.get("player_state")
        original_volume = self._get_player_volume()
        baseline_volume = int(self.args.duck_baseline_volume)
        set_status, set_payload = self._set_player_volume(baseline_volume)
        if set_status != 200:
            return StageResult(
                "music_ducking_real_output_level",
                "fail",
                details={
                    "error": "failed to set player baseline volume for output-level ducking proof",
                    "set_status_code": set_status,
                    "set_response": set_payload,
                    "original_volume": original_volume,
                    "baseline_volume": baseline_volume,
                },
            )
        log_path = self._latest_log_path()
        offset = log_path.stat().st_size if log_path and log_path.exists() else 0
        captures: dict[str, Any] = {}
        source_samples: list[dict[str, Any]] = []
        baseline_snapshot: dict[str, Any] = {}
        ducked_snapshot: dict[str, Any] = {}
        restored_snapshot: dict[str, Any] = {}
        try:
            time.sleep(float(self.args.duck_settle_s))
            baseline_snapshot = self._player_state_snapshot()
            source_samples.append(baseline_snapshot)
            captures["baseline"] = self._capture_loopback_window(
                self.ctx.report_dir / "ducking_baseline_output.wav",
                duration_s=float(self.args.duck_output_window_s),
                preferred_output=runtime_output,
            )
            duck_status, duck_payload = self._http_post_json("/v1/audio/duck", {}, timeout=5)
            time.sleep(float(self.args.duck_settle_s))
            ducked_snapshot = self._player_state_snapshot()
            source_samples.append(ducked_snapshot)
            captures["ducked"] = self._capture_loopback_window(
                self.ctx.report_dir / "ducking_ducked_output.wav",
                duration_s=float(self.args.duck_output_window_s),
                preferred_output=runtime_output,
            )
            unduck_status, unduck_payload = self._http_post_json("/v1/audio/unduck", {}, timeout=5)
            time.sleep(float(self.args.duck_settle_s))
            restored_snapshot = self._player_state_snapshot()
            source_samples.append(restored_snapshot)
            captures["restored"] = self._capture_loopback_window(
                self.ctx.report_dir / "ducking_restored_output.wav",
                duration_s=float(self.args.duck_output_window_s),
                preferred_output=runtime_output,
            )
        finally:
            if original_volume is not None:
                with contextlib.suppress(Exception):
                    self._set_player_volume(int(original_volume))
        baseline_rms = float(captures.get("baseline", {}).get("rms") or 0.0)
        ducked_rms = float(captures.get("ducked", {}).get("rms") or 0.0)
        restored_rms = float(captures.get("restored", {}).get("rms") or 0.0)
        baseline_ok = baseline_rms >= float(self.args.min_music_output_rms)
        ducked_output_drop_observed = bool(
            baseline_rms > 0 and ducked_rms <= baseline_rms * float(self.args.max_ducked_output_ratio)
        )
        restored_output_audible = restored_rms >= float(self.args.min_music_output_rms)
        api_ok = duck_status == 200 and unduck_status == 200
        new_text = self._read_from_offset(log_path, offset) if log_path else ""
        log_excerpt = self._matching_lines(new_text, DUCK_PATTERNS)[-12:]
        source_contract = assert_ducking_from_volume_samples(
            music_playing_at_baseline=bool(baseline_snapshot.get("is_playing")),
            baseline_volume=baseline_volume,
            original_volume=original_volume,
            samples=source_samples,
            min_attenuation_delta=float(self.args.min_duck_attenuation_delta),
            restore_tolerance=float(self.args.duck_restore_tolerance),
            log_lines=log_excerpt,
        )
        ok = baseline_ok and restored_output_audible and api_ok and source_contract.ok
        details = {
            "route": route,
            "runtime_output": runtime_output,
            "player_state": player_state,
            "playback_start": playback_start,
            "original_volume": original_volume,
            "baseline_volume": baseline_volume,
            "source_samples": source_samples,
            "source_volume_contract": source_contract.details,
            "set_status_code": set_status,
            "set_response": set_payload,
            "duck_status_code": duck_status,
            "duck_response": duck_payload,
            "unduck_status_code": unduck_status,
            "unduck_response": unduck_payload,
            "captures": captures,
            "baseline_rms": baseline_rms,
            "ducked_rms": ducked_rms,
            "restored_rms": restored_rms,
            "ratios": {
                "ducked_to_baseline": (ducked_rms / baseline_rms if baseline_rms else None),
                "restored_to_baseline": (restored_rms / baseline_rms if baseline_rms else None),
            },
            "checks": {
                "baseline_ok": baseline_ok,
                "restored_output_audible": restored_output_audible,
                "source_volume_ducked_and_restored": source_contract.ok,
                "ducked_output_drop_observed": ducked_output_drop_observed,
                "api_ok": api_ok,
            },
            "output_measurement_note": (
                "Loopback RMS is total output context only; ducking pass/fail is isolated to "
                "the music source volume because total RMS can include different music windows "
                "or TTS overlap."
            ),
            "log_excerpt": log_excerpt,
        }
        self.ctx.ducking_output_level = details
        return StageResult(
            "music_ducking_real_output_level",
            "pass" if ok else "fail",
            details=details,
            evidence=[
                "source_volume baseline=%s ducked=%s restored=%s contract_ok=%s"
                % (
                    baseline_snapshot.get("volume"),
                    ducked_snapshot.get("volume"),
                    restored_snapshot.get("volume"),
                    source_contract.ok,
                ),
                "baseline_rms=%.1f ducked_rms=%.1f restored_rms=%.1f" % (baseline_rms, ducked_rms, restored_rms),
                "total_loopback_ducked_ratio=%s restored_ratio=%s"
                % (
                    details["ratios"]["ducked_to_baseline"],
                    details["ratios"]["restored_to_baseline"],
                ),
            ],
        )

    def _music_ducking_real_voice_turn(self) -> StageResult:
        status, player_state = self._http_get_json("/v1/player/state", timeout=5)
        state = player_state.get("data", player_state) if isinstance(player_state, dict) else {}
        is_playing = bool(state.get("is_playing")) if isinstance(state, dict) else False
        if status != 200 or not is_playing:
            return StageResult(
                "music_ducking_real_voice_turn",
                "fail",
                details={
                    "missing_capability": "active music playback is required for ducking proof",
                    "status_code": status,
                    "player_state": player_state,
                },
            )
        text = self.ctx.live_roundtrip_log
        if not text:
            return StageResult(
                "music_ducking_real_voice_turn",
                "fail",
                details={"missing_capability": "live voice-turn log required before judging ducking"},
            )
        duck_lines = self._matching_lines(text, DUCK_PATTERNS)
        contract = assert_ducking_from_volume_samples(
            music_playing_at_baseline=is_playing,
            baseline_volume=self.ctx.voice_side_effect.get("baseline_volume"),
            original_volume=self.ctx.voice_side_effect.get("after_volume"),
            samples=self.ctx.ducking_volume_samples,
            min_attenuation_delta=float(self.args.min_duck_attenuation_delta),
            restore_tolerance=float(self.args.duck_restore_tolerance),
            log_lines=duck_lines,
        )
        return StageResult(
            "music_ducking_real_voice_turn",
            "pass" if contract.ok else "fail",
            details={
                "status_code": status,
                "player_state": player_state,
                "duck_lines": duck_lines,
                "volume_samples": self.ctx.ducking_volume_samples,
                "ducking_contract": contract.details,
            },
            evidence=duck_lines,
        )

    def _recovery_paths(self) -> StageResult:
        checks: dict[str, Any] = {}
        silence = self._silence_wav(duration_s=1.5)
        url = f"{self.ctx.base_url}/v1/transcribe"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=float(self.args.recovery_timeout_s)) as client:
                with silence.open("rb") as fh:
                    response = client.post(
                        url,
                        headers=headers,
                        files={"audio": (silence.name, fh, "audio/wav")},
                    )
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            payload: dict[str, Any] | str
            try:
                payload = response.json()
            except Exception:
                payload = response.text[:1000]
            transcript = self._extract_transcript(payload)
            error_code = self._extract_error_code(payload)
            silence_contract = assert_silence_stt_recovery(
                transcript=transcript,
                status_code=response.status_code,
                error_code=error_code,
                elapsed_ms=elapsed_ms,
                timeout_s=float(self.args.recovery_timeout_s),
            )
            checks["stt_silence_timeout_no_hang"] = {
                "ok": silence_contract.ok,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                "transcript": transcript,
                "accepted_no_speech_error": silence_contract.details["accepted_no_speech_error"],
                "silence_contract": silence_contract.details,
                "response": payload,
            }
        except Exception as exc:
            checks["stt_silence_timeout_no_hang"] = {"ok": False, "error": repr(exc)}

        ok = all(bool(item.get("ok")) for item in checks.values())
        return StageResult(
            "recovery_paths",
            "pass" if ok else "fail",
            details={"checks": checks},
            evidence=["%s ok=%s" % (name, item.get("ok")) for name, item in checks.items()],
        )

    def _live_audio_route(self) -> StageResult:
        route = self.ctx.live_audio_route
        if not route:
            if self.ctx.headless_injection_limit:
                return StageResult(
                    "live_audio_route",
                    "skip",
                    details={
                        "missing_capability": "no calibrated route because headless acoustic injection stayed below the live-mic RMS floor",
                        "headless_injection_limit": self.ctx.headless_injection_limit,
                    },
                    evidence=[
                        "route skipped: wake model accepted but headless mic RMS below route floor",
                    ],
                )
            return StageResult(
                "live_audio_route",
                "fail",
                details={"error": "no calibrated audio route"},
            )
        listener_input = route["listener_input"]
        runtime_output = route.get("runtime_output")
        original = {
            "input_device": self.ctx.settings.get("input_device", ""),
            "output_device": self.ctx.settings.get("output_device", ""),
        }
        log_path = self._latest_log_path()
        offset = log_path.stat().st_size if log_path and log_path.exists() else 0
        recent_log_text = self._tail_text(log_path, max_chars=200_000) if log_path else ""
        input_name = str(listener_input["name"]).lower()
        input_index = str(listener_input["index"])
        input_seen_before = input_index in recent_log_text or input_name in recent_log_text.lower()
        desired: dict[str, str] = {}
        if not input_seen_before:
            desired["input_device"] = input_index
        if runtime_output:
            output_name = str(runtime_output["name"])
            configured_output = str(original.get("output_device") or "").lower()
            if not (
                configured_output
                and (configured_output in output_name.lower() or output_name.lower() in configured_output)
            ):
                desired["output_device"] = output_name
        self.ctx.audio_route_restore = {
            "original": {key: value for key, value in original.items() if key in desired},
            "desired": desired,
            "restored": False,
        }
        if desired:
            status, payload = self._http_post_json("/v1/settings", {"settings": desired}, timeout=10)
            time.sleep(float(self.args.route_restart_wait_s))
            self.ctx.settings.update(desired)
            log_text = self._read_from_offset(log_path, offset) if log_path else ""
        else:
            status = 200
            payload = {
                "skipped": True,
                "reason": "calibrated listener already active and runtime output unchanged",
            }
            log_text = ""
        route_log = self._matching_lines(
            recent_log_text + log_text,
            ("AUDIO_DEVICE:", "Using configured input device:"),
        )[-12:]
        combined_log_text = recent_log_text + log_text
        runtime_device_seen = input_index in combined_log_text or input_name in combined_log_text.lower()
        ok = status == 200 and runtime_device_seen
        evidence_output = str(runtime_output["name"]) if runtime_output else "<current-runtime-output>"
        return StageResult(
            "live_audio_route",
            "pass" if ok else "fail",
            details={
                "route": route,
                "original_settings": original,
                "desired_settings": desired,
                "input_seen_before_settings": input_seen_before,
                "status_code": status,
                "response": payload,
                "runtime_device_seen": runtime_device_seen,
                "log_excerpt": route_log,
            },
            evidence=[
                "route_kind=%s input=%s output=%s" % (route["kind"], listener_input["name"], evidence_output),
                "settings_status=%s runtime_device_seen=%s" % (status, runtime_device_seen),
            ]
            + route_log,
        )

    def _restore_audio_route(self) -> dict[str, Any]:
        restore = dict(self.ctx.audio_route_restore or {})
        original = restore.get("original")
        desired = restore.get("desired")
        if not isinstance(original, dict) or not desired:
            return {"status": "skip", "reason": "no route settings changed"}
        try:
            status, payload = self._http_post_json("/v1/settings", {"settings": original}, timeout=10)
            restore.update(
                {
                    "status": "restored" if status == 200 else "failed",
                    "status_code": status,
                    "response": payload,
                }
            )
            restore["restored"] = status == 200
            return restore
        except Exception as exc:
            restore.update({"status": "error", "error": repr(exc), "restored": False})
            return restore

    def _live_injection_route_calibration(self) -> StageResult:
        wake_wavs = self._ranked_wake_wavs()
        if not wake_wavs:
            return StageResult(
                "live_injection_route_calibration",
                "fail",
                details={"error": "no wake wav"},
            )
        routes = self.ctx.audio_route_candidates or self._audio_route_candidates()
        if not routes:
            return StageResult(
                "live_injection_route_calibration",
                "fail",
                details={
                    "error": "no audio route candidates",
                    "devices": self.ctx.devices,
                },
            )
        attempts: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
        max_attempts = max(1, int(self.args.max_calibration_attempts))
        attempt_count = 0
        selected: dict[str, Any] | None = None
        route_items = list(enumerate(routes))
        ranked_wavs = wake_wavs[: max(1, int(self.args.max_calibration_wavs))]
        gains = self._calibration_gains()
        for wav_path in ranked_wavs:
            for gain in gains:
                for route_index, route in route_items:
                    if attempt_count >= max_attempts:
                        break
                    attempt_count += 1
                    capture_path = self.ctx.report_dir / (
                        "wake_injection_%02d_%s_g%s.wav"
                        % (
                            attempt_count,
                            str(route["kind"]).replace(" ", "_"),
                            str(gain).replace(".", "p"),
                        )
                    )
                    attempt: dict[str, Any] = {
                        "route_index": route_index,
                        "route_kind": route["kind"],
                        "wake_wav": str(wav_path),
                        "gain": gain,
                        "listener_input": route["listener_input"],
                        "injection_output": route["injection_output"],
                    }
                    try:
                        capture = self._playrec_wav(
                            wav_path,
                            input_index=int(route["listener_input"]["index"]),
                            output_index=int(route["injection_output"]["index"]),
                            out_path=capture_path,
                            gain=float(gain),
                        )
                        score = self._score_wake_audio(capture_path)
                        source_score = self._score_wake_audio(wav_path)
                        capture["recorded_wake_score"] = score
                        capture["source_wake_score"] = source_score
                        attempt.update(capture)
                    except Exception as exc:
                        attempt["error"] = repr(exc)
                    attempt["rms_ok"] = float(attempt.get("rms") or 0.0) >= float(self.args.min_injection_rms)
                    attempt["score_ok"] = float(attempt.get("recorded_wake_score") or 0.0) >= float(
                        self.args.wake_threshold
                    )
                    attempt["peak_ok"] = float(attempt.get("peak") or 0.0) <= float(self.args.max_injection_peak)
                    attempt["passed"] = bool(attempt["rms_ok"] and attempt["score_ok"] and attempt["peak_ok"])
                    attempts.append(attempt)
                    if best is None or self._calibration_rank(attempt) > self._calibration_rank(best):
                        best = attempt
                    if attempt["passed"]:
                        selected = {**route, "calibration": attempt}
                        break
                if selected or attempt_count >= max_attempts:
                    break
            if selected or attempt_count >= max_attempts:
                break
        if selected:
            self.ctx.live_audio_route = selected
            self.ctx.selected_wake_wav = Path(str(selected["calibration"]["wake_wav"]))
            self.ctx.selected_injection_gain = float(selected["calibration"]["gain"])
        if not selected and best:
            best_score_ok = float(best.get("recorded_wake_score") or 0.0) >= float(self.args.wake_threshold)
            best_peak_ok = float(best.get("peak") or 0.0) <= float(self.args.max_injection_peak)
            best_rms = float(best.get("rms") or 0.0)
            if best_score_ok and best_peak_ok and best_rms < float(self.args.min_injection_rms):
                self.ctx.headless_injection_limit = {
                    "reason": "headless acoustic injection wake model accepted, but captured mic RMS is below the live-mic route floor",
                    "harness_limited": True,
                    "safety_gates_preserved": True,
                    "wake_wav": best.get("wake_wav"),
                    "gain": best.get("gain"),
                    "route_kind": best.get("route_kind"),
                    "listener_input": best.get("listener_input"),
                    "injection_output": best.get("injection_output"),
                    "captured_rms": best_rms,
                    "min_injection_rms": float(self.args.min_injection_rms),
                    "recorded_wake_score": float(best.get("recorded_wake_score") or 0.0),
                    "source_wake_score": float(best.get("source_wake_score") or 0.0),
                    "wake_threshold": float(self.args.wake_threshold),
                    "peak": float(best.get("peak") or 0.0),
                    "max_injection_peak": float(self.args.max_injection_peak),
                    "runtime_policy_note": "product wake zero-input mic_rms floor remains unchanged; full live-mic RMS roundtrip is a documented headless-harness skip",
                }
        status = "pass" if selected else ("skip" if self.ctx.headless_injection_limit else "fail")
        selected_details = selected if selected else None
        return StageResult(
            "live_injection_route_calibration",
            status,
            details={
                "selected_route": selected_details,
                "best_attempt": best,
                "attempts": attempts,
                "headless_injection_limit": self.ctx.headless_injection_limit,
                "min_injection_rms": self.args.min_injection_rms,
                "wake_threshold": self.args.wake_threshold,
                "max_injection_peak": self.args.max_injection_peak,
                "attempt_count": attempt_count,
            },
            evidence=[
                "selected_route=%s" % (selected["kind"] if selected else "<none>"),
                "best_rms=%.1f" % float((best or {}).get("rms") or 0.0),
                "best_recorded_wake_score=%.3f" % float((best or {}).get("recorded_wake_score") or 0.0),
                ("headless_harness_limit=%s safety_gates_preserved=True" % bool(self.ctx.headless_injection_limit)),
            ],
        )

    def _ranked_wake_wavs(self) -> list[Path]:
        if self.ctx.wake_scores:
            existing = [
                (float(item.get("score") or 0.0), Path(str(item.get("path"))))
                for item in self.ctx.wake_scores
                if item.get("path")
            ]
            return [path for _score, path in sorted(existing, reverse=True) if path.exists()]
        scored: list[tuple[float, Path]] = []
        for path in self._wake_wavs()[: self.args.max_wake_scan]:
            with contextlib.suppress(Exception):
                scored.append((self._score_wake_audio(path), path))
        return [path for _score, path in sorted(scored, reverse=True)]

    def _calibration_gains(self) -> list[float]:
        values = self.args.calibration_gain
        if not values:
            values = list(DEFAULT_INJECTION_GAINS)
        gains = []
        for raw in values:
            gain = float(raw)
            if gain > 0 and gain not in gains:
                gains.append(gain)
        return gains or list(DEFAULT_INJECTION_GAINS)

    @staticmethod
    def _calibration_rank(attempt: dict[str, Any]) -> tuple[int, float, float, float]:
        return (
            1 if attempt.get("passed") else 0,
            float(attempt.get("recorded_wake_score") or 0.0),
            float(attempt.get("rms") or 0.0),
            -float(attempt.get("peak") or 0.0),
        )

    def _live_virtual_mic_roundtrip(self) -> StageResult:
        route = self.ctx.live_audio_route
        if not route:
            if self.ctx.headless_injection_limit:
                skip_capture = {
                    "status": "skip",
                    "reason": "known_headless_harness_limit",
                    "missing_capability": "full live-mic RMS roundtrip requires mic RMS above runtime zero-input and oracle route floors",
                    "headless_injection_limit": self.ctx.headless_injection_limit,
                    "capability_proofs": {
                        "wake_model_real_score": "see wake_model_real_score stage",
                        "stt_live_transcribe": "see stt_live_transcribe stage",
                    },
                    "rms_gate_changed": False,
                    "safety_gates_preserved": True,
                }
                self.ctx.live_roundtrip_tts_capture = skip_capture
                return StageResult(
                    "live_virtual_mic_roundtrip",
                    "skip",
                    details=skip_capture,
                    evidence=[
                        "live-mic RMS roundtrip skipped: wake model accepted but headless mic RMS below route floor",
                        "safety gates preserved; capability proof is wake_model_real_score + stt_live_transcribe",
                    ],
                )
            return StageResult(
                "live_virtual_mic_roundtrip",
                "fail",
                details={"error": "no calibrated audio route"},
            )
        wake_wav = self.ctx.selected_wake_wav
        if wake_wav is None or not wake_wav.exists():
            return StageResult("live_virtual_mic_roundtrip", "fail", details={"error": "no wake wav"})
        command_wav = self._command_wav()
        if command_wav is None or not command_wav.exists():
            return StageResult(
                "live_virtual_mic_roundtrip",
                "fail",
                details={"error": "real command wav unavailable"},
            )
        injection_output = route["injection_output"]
        listener_input = route["listener_input"]
        runtime_output = self._runtime_capture_output(route)
        configured_input = str(self.ctx.settings.get("input_device", "") or "")
        expected_index = str(listener_input["index"])
        expected_name = str(listener_input["name"])
        log_path = self._latest_log_path()
        if log_path is None:
            return StageResult("live_virtual_mic_roundtrip", "fail", details={"error": "no log file"})
        recent_log_text = self._tail_text(log_path, max_chars=200_000)
        runtime_device_ok = (
            expected_name.lower() in recent_log_text.lower() or ("index=%s" % expected_index) in recent_log_text
        )
        if (
            configured_input
            and configured_input not in (expected_index, expected_name)
            and expected_name.lower() not in configured_input.lower()
        ):
            return StageResult(
                "live_virtual_mic_roundtrip",
                "fail",
                details={
                    "reason": "running settings are not pointed at the calibrated listener input",
                    "configured_input": configured_input,
                    "expected_index": expected_index,
                    "expected_name": expected_name,
                    "route_kind": route["kind"],
                    "runtime_device_ok": runtime_device_ok,
                },
            )
        if not configured_input and not runtime_device_ok:
            return StageResult(
                "live_virtual_mic_roundtrip",
                "fail",
                details={
                    "reason": "input_device is default/blank, but recent logs do not show the expected runtime input device",
                    "configured_input": configured_input,
                    "expected_name": expected_name,
                    "route_kind": route["kind"],
                    "runtime_device_ok": runtime_device_ok,
                },
            )
        original_volume = self._get_player_volume()
        if original_volume is None:
            return StageResult(
                "live_virtual_mic_roundtrip",
                "fail",
                details={"missing_capability": "player volume state unavailable for voice-path side-effect proof"},
            )
        baseline_volume = int(self.args.intent_baseline_volume)
        set_status, set_payload = self._set_player_volume(baseline_volume)
        if set_status != 200:
            return StageResult(
                "live_virtual_mic_roundtrip",
                "fail",
                details={
                    "error": "failed to set baseline volume for voice-path side-effect proof",
                    "set_status_code": set_status,
                    "set_response": set_payload,
                    "original_volume": original_volume,
                    "baseline_volume": baseline_volume,
                },
            )
        baseline_state = self._player_state_snapshot()
        self.ctx.voice_side_effect = {
            "original_volume": original_volume,
            "baseline_volume": baseline_volume,
            "baseline_player_state": baseline_state,
            "set_status_code": set_status,
            "set_response": set_payload,
        }
        offset = log_path.stat().st_size
        start = time.perf_counter()
        volume_samples: list[dict[str, Any]] = []
        capture_path = self.ctx.report_dir / "voice_turn_tts_loopback.wav"
        self.ctx.tts_loopback_wav = capture_path
        wake_seen: dict[str, Any] = {"matched": False}
        final: dict[str, Any] = {"matched": False}
        tts_trigger: dict[str, Any] = {"matched": False}
        command_played = False
        capture = {"status": "not_started", "path": str(capture_path), "rms": 0.0}
        turn_errors: list[str] = []
        wake_gain = float(self.ctx.selected_injection_gain or self.args.injection_gain)
        command_gain = float(self.args.command_injection_gain)
        if command_gain <= 0.0:
            command_gain = wake_gain

        try:
            self._play_wav(wake_wav, int(injection_output["index"]), gain=wake_gain)
            wake_seen = self._wait_for_patterns(
                log_path,
                offset,
                WAKE_POLICY_ALLOW_PATTERNS,
                timeout_s=float(self.args.wake_wait_s),
            )
            if wake_seen["matched"]:
                time.sleep(float(self.args.command_delay_s))
                self.ctx.command_started_at = time.time()
                self._play_wav(command_wav, int(injection_output["index"]), gain=command_gain)
                self.ctx.command_finished_at = time.time()
                command_played = True
            tts_trigger = self._wait_for_patterns(
                log_path,
                offset,
                TTS_PATTERNS + ("STT returned empty transcript",),
                timeout_s=float(self.args.roundtrip_wait_s),
            )
            if tts_trigger["matched"] and tts_trigger.get("pattern") != "STT returned empty transcript":
                time.sleep(float(self.args.tts_capture_start_delay_s))
                capture = self._capture_loopback_window(
                    capture_path,
                    duration_s=float(self.args.tts_capture_window_s),
                    preferred_output=runtime_output,
                    exclude_names=([str(injection_output["name"])] if route["kind"] == "virtual" else []),
                )
            final = self._wait_for_patterns(
                log_path,
                offset,
                STT_PATTERNS + TTS_PATTERNS + ("UI: Voice state -> IDLE", "STT returned empty transcript"),
                timeout_s=3.0,
            )
        except Exception as exc:
            self.ctx.command_finished_at = time.time()
            turn_errors.append(repr(exc))
            capture = {
                "status": "error",
                "error": repr(exc),
                "path": str(capture_path),
                "rms": 0.0,
            }
        self.ctx.ducking_volume_samples = volume_samples
        capture["command_finished_at"] = self.ctx.command_finished_at
        capture["capture_started_after_command"] = bool(
            capture.get("capture_started_at")
            and self.ctx.command_finished_at
            and capture["capture_started_at"] > self.ctx.command_finished_at
        )
        self.ctx.live_roundtrip_tts_capture = capture
        tts_transcribe = (
            self._transcribe_wav_via_running_stt(capture_path, stage="live_virtual_mic_roundtrip")
            if capture.get("status") == "captured"
            else {"transcript": "", "skipped": capture.get("status")}
        )
        self.ctx.live_roundtrip_tts_transcript = str(tts_transcribe.get("transcript") or "")
        tts_content = assert_tts_audible_content(
            transcript=self.ctx.live_roundtrip_tts_transcript,
            expected_terms=self.args.expected_tts_terms,
            rms=float(capture.get("rms") or 0.0),
            min_rms=float(self.args.min_loopback_rms),
        )
        after_volume = self._get_player_volume()
        side_effect_ok = after_volume is not None and after_volume > baseline_volume
        self.ctx.voice_side_effect.update(
            {
                "after_volume": after_volume,
                "side_effect_ok": side_effect_ok,
                "command_played": command_played,
            }
        )
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        text = self._read_from_offset(log_path, offset)
        self.ctx.live_roundtrip_log = text
        matching = self._matching_lines(
            text,
            WAKE_PATTERNS
            + WAKE_POLICY_ALLOW_PATTERNS
            + WAKE_DIAG_PATTERNS
            + STT_PATTERNS
            + TTS_PATTERNS
            + VOICE_HANDLER_PATTERNS
            + DUCK_PATTERNS
            + INTENT_PATTERNS
            + FORCE_WAKE_PATTERNS,
        )
        policy = self._wake_policy_snapshot(text)
        stt_lines = self._matching_lines(text, STT_PATTERNS)
        transcript_seen = bool(stt_lines) and not any("STT returned empty transcript" in line for line in stt_lines)
        voice_handler_eval = evaluate_positive_log_lines(text, VOICE_HANDLER_PATTERNS)
        intent_eval = evaluate_positive_log_lines(text, INTENT_PATTERNS)
        tts_log_eval = evaluate_positive_log_lines(text, TTS_PATTERNS)
        intent_seen = bool(intent_eval["accepted"])
        tts_seen = bool(tts_log_eval["accepted"])
        rejected_positive_lines = (
            voice_handler_eval["rejected_lines"] + intent_eval["rejected_lines"] + tts_log_eval["rejected_lines"]
        )
        force_wake_seen = any(pattern in text for pattern in FORCE_WAKE_PATTERNS)
        calibration = route.get("calibration") if isinstance(route.get("calibration"), dict) else {}
        calibration_score_ok = float(calibration.get("recorded_wake_score") or 0.0) >= float(self.args.wake_threshold)
        zero_input_failed = "zero_input" in set(policy.get("failed_required_layers") or [])
        acoustic_headless_not_accepted = (
            route.get("kind") == "acoustic_fallback"
            and calibration_score_ok
            and not wake_seen["matched"]
            and not command_played
        )
        if (
            acoustic_headless_not_accepted
            and (zero_input_failed or not policy.get("allow_seen"))
            and not force_wake_seen
        ):
            with contextlib.suppress(Exception):
                self._set_player_volume(int(original_volume))
            self.ctx.headless_injection_limit = {
                "reason": (
                    "runtime wake did not accept headless acoustic playback even though calibration "
                    "recorded a wake-model-accepted sample"
                ),
                "harness_limited": True,
                "safety_gates_preserved": True,
                "calibration": calibration,
                "runtime_policy": policy,
                "zero_input_failed": zero_input_failed,
                "runtime_allow_seen": bool(policy.get("allow_seen")),
                "route_kind": route.get("kind"),
                "listener_input": listener_input,
                "injection_output": injection_output,
                "runtime_output": runtime_output,
                "wake_seen": wake_seen,
            }
            capture.update(
                {
                    "status": "skip",
                    "reason": "known_headless_harness_limit",
                    "missing_capability": "full live-mic RMS roundtrip requires reliable real-mic acoustic energy, which this headless route does not provide",
                    "headless_injection_limit": self.ctx.headless_injection_limit,
                    "rms_gate_changed": False,
                    "safety_gates_preserved": True,
                }
            )
            self.ctx.live_roundtrip_tts_capture = capture
            return StageResult(
                "live_virtual_mic_roundtrip",
                "skip",
                latency_ms=elapsed,
                details={
                    "wake_wav": str(wake_wav),
                    "wake_gain": wake_gain,
                    "command_wav": str(command_wav),
                    "command_gain": command_gain,
                    "route": route,
                    "wake_seen": wake_seen,
                    "policy": policy,
                    "tts_capture": capture,
                    "side_effect": self.ctx.voice_side_effect,
                    "turn_errors": turn_errors,
                    "log_path": str(log_path),
                    "log_excerpt": matching[-30:],
                    "headless_injection_limit": self.ctx.headless_injection_limit,
                },
                evidence=[
                    "runtime did not accept headless acoustic playback after recorded wake score %.3f"
                    % float(calibration.get("recorded_wake_score") or 0.0),
                    "safety gates preserved; wake_model_real_score + stt_live_transcribe remain capability proof",
                ]
                + matching[-10:],
            )
        ok = (
            bool(wake_seen["matched"])
            and command_played
            and policy["all_layers_passed"]
            and not force_wake_seen
            and transcript_seen
            and bool(voice_handler_eval["accepted"])
            and intent_seen
            and tts_seen
            and capture.get("status") == "captured"
            and capture.get("capture_started_after_command")
            and tts_content.ok
            and side_effect_ok
            and not rejected_positive_lines
            and not turn_errors
        )
        return StageResult(
            "live_virtual_mic_roundtrip",
            "pass" if ok else "fail",
            latency_ms=elapsed,
            details={
                "wake_wav": str(wake_wav),
                "wake_gain": wake_gain,
                "command_wav": str(command_wav),
                "command_gain": command_gain,
                "route": route,
                "injection_output": injection_output,
                "listener_input": listener_input,
                "runtime_output": runtime_output,
                "wake_seen": wake_seen,
                "tts_trigger": tts_trigger,
                "final_seen": final,
                "policy": policy,
                "transcript_seen": transcript_seen,
                "voice_handler_evidence": voice_handler_eval,
                "intent_seen": intent_seen,
                "intent_evidence": intent_eval,
                "tts_seen": tts_seen,
                "tts_log_evidence": tts_log_eval,
                "tts_capture": capture,
                "tts_transcribe": tts_transcribe,
                "tts_content_contract": tts_content.details,
                "force_wake_seen": force_wake_seen,
                "rejected_positive_lines": rejected_positive_lines,
                "side_effect": self.ctx.voice_side_effect,
                "volume_samples": volume_samples,
                "turn_errors": turn_errors,
                "log_path": str(log_path),
                "log_excerpt": matching[-30:],
            },
            evidence=matching
            + [
                "volume_before=%s volume_after=%s side_effect_ok=%s" % (baseline_volume, after_volume, side_effect_ok),
                "tts_capture_status=%s rms=%.1f transcript=%r"
                % (
                    capture.get("status"),
                    float(capture.get("rms") or 0.0),
                    self.ctx.live_roundtrip_tts_transcript,
                ),
            ],
        )

    def _latest_log_path(self) -> Path | None:
        existing = [self.ctx.root / candidate for candidate in LOG_CANDIDATES if (self.ctx.root / candidate).exists()]
        if not existing:
            return None
        return max(existing, key=lambda path: path.stat().st_mtime)

    def _log_text_since_start(self, path: Path, max_chars: int) -> str:
        text = self._tail_text(path, max_chars=max_chars)
        started_local = datetime.fromtimestamp(self.ctx.started_at)
        fresh_lines: list[str] = []
        for line in text.splitlines():
            match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", line)
            if not match:
                continue
            try:
                line_dt = datetime.fromisoformat("%sT%s" % (match.group(1), match.group(2)))
            except ValueError:
                continue
            if line_dt >= started_local:
                fresh_lines.append(line)
        return "\n".join(fresh_lines)

    def _trace_root_candidates(self) -> list[Path]:
        return [
            self.ctx.root / ".viola" / "traces" / "by_user",
            self.ctx.root / "data" / "traces" / "by_user",
            self.ctx.root / "logs" / "traces",
        ]

    @staticmethod
    def _find_nested_value(payload: Any, key: str) -> Any:
        if isinstance(payload, dict):
            if key in payload:
                return payload[key]
            for value in payload.values():
                found = Oracle._find_nested_value(value, key)
                if found not in (None, ""):
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = Oracle._find_nested_value(value, key)
                if found not in (None, ""):
                    return found
        return None

    def _trace_files_between(self, after: float, before: float) -> list[Path]:
        paths: list[Path] = []
        for root in self._trace_root_candidates():
            if not root.exists():
                continue
            for path in root.rglob("*.trace.jsonl*"):
                if "blobs" in path.parts or "keys" in path.parts:
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if after - 1.0 <= mtime <= before + 1.0:
                    paths.append(path)
        return sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)

    def _get_player_volume(self) -> int | None:
        status, payload = self._http_get_json("/v1/player/state", timeout=5)
        if status != 200 or not isinstance(payload, dict):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return None
        for key in ("volume", "hub_local_volume"):
            value = data.get(key)
            if isinstance(value, (int, float, str)):
                try:
                    return max(0, min(100, int(value)))
                except ValueError:
                    continue
        playback = (self.ctx.diagnostics or {}).get("playback_state")
        if isinstance(playback, dict):
            value = playback.get("volume")
            if isinstance(value, (int, float, str)):
                try:
                    return max(0, min(100, int(value)))
                except ValueError:
                    return None
        return None

    def _set_player_volume(self, level: int) -> tuple[int, dict[str, Any] | str]:
        return self._http_post_json("/v1/volume", {"level": int(level)}, timeout=10)

    def _ensure_music_playing_for_ducking(self, *, runtime_output: dict[str, Any] | None) -> dict[str, Any]:
        initial_status, initial_payload = self._http_get_json("/v1/player/state", timeout=5)
        initial_state = initial_payload.get("data", initial_payload) if isinstance(initial_payload, dict) else {}
        initial_playing = bool(initial_state.get("is_playing")) if isinstance(initial_state, dict) else False
        force_restart = bool(self.args.ducking_restart_playback)
        result: dict[str, Any] = {
            "initial_status_code": initial_status,
            "initial_state": initial_state,
            "initial_is_playing": initial_playing,
            "force_restart": force_restart,
            "runtime_output": runtime_output,
            "play_query": self.args.ducking_play_query,
            "play_source": self.args.ducking_play_source,
            "polls": [],
        }
        if initial_status == 200 and initial_playing and not force_restart:
            result.update(
                {
                    "ok": True,
                    "action": "already_playing",
                    "player_state": initial_payload,
                }
            )
            return result
        if not self.args.ducking_play_query:
            result.update({"ok": False, "reason": "ducking_play_query_missing"})
            return result
        if force_restart and initial_playing:
            stop_status, stop_payload = self._http_post_json("/v1/stop", {}, timeout=10)
            result.update({"stop_status_code": stop_status, "stop_response": stop_payload})
            time.sleep(0.5)
        play_payload: dict[str, Any] = {"query": str(self.args.ducking_play_query)}
        if self.args.ducking_play_source:
            play_payload["source"] = str(self.args.ducking_play_source)
        play_status, play_response = self._http_post_json(
            "/v1/play",
            play_payload,
            timeout=max(10.0, float(self.args.ducking_start_timeout_s)),
        )
        result.update(
            {
                "action": "started_playback",
                "play_status_code": play_status,
                "play_response": play_response,
            }
        )
        deadline = time.time() + max(1.0, float(self.args.ducking_start_timeout_s))
        last_payload: dict[str, Any] | str = {}
        while time.time() < deadline:
            state_status, state_payload = self._http_get_json("/v1/player/state", timeout=5)
            last_payload = state_payload
            state = state_payload.get("data", state_payload) if isinstance(state_payload, dict) else {}
            is_playing = bool(state.get("is_playing")) if isinstance(state, dict) else False
            now_playing = state.get("now_playing") if isinstance(state, dict) else None
            result["polls"].append(
                {
                    "status_code": state_status,
                    "is_playing": is_playing,
                    "title": ((now_playing or {}).get("title") if isinstance(now_playing, dict) else None),
                    "position": (state.get("position") if isinstance(state, dict) else None),
                    "volume": state.get("volume") if isinstance(state, dict) else None,
                }
            )
            if state_status == 200 and is_playing and now_playing:
                result.update({"ok": True, "player_state": state_payload})
                return result
            time.sleep(0.5)
        result.update(
            {
                "ok": False,
                "reason": "playback_not_observed",
                "last_player_state": last_payload,
            }
        )
        return result

    def _player_state_snapshot(self) -> dict[str, Any]:
        status, payload = self._http_get_json("/v1/player/state", timeout=5)
        state = payload.get("data", payload) if isinstance(payload, dict) else {}
        volume = None
        if isinstance(state, dict):
            for key in ("volume", "hub_local_volume"):
                value = state.get(key)
                if value is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        volume = float(value)
                        break
        return {
            "ts": time.time(),
            "status_code": status,
            "is_playing": (bool(state.get("is_playing")) if isinstance(state, dict) else False),
            "volume": volume,
            "state": state,
        }

    def _start_player_sampler(
        self,
    ) -> tuple[list[dict[str, Any]], threading.Event, threading.Thread]:
        samples: list[dict[str, Any]] = []
        stop = threading.Event()

        def sample() -> None:
            while not stop.is_set():
                samples.append(self._player_state_snapshot())
                stop.wait(max(0.1, float(self.args.duck_sample_interval_s)))

        thread = threading.Thread(target=sample, name="voice-oracle-player-sampler", daemon=True)
        thread.start()
        return samples, stop, thread

    def _transcribe_wav_via_running_stt(self, wav_path: Path, *, stage: str) -> dict[str, Any]:
        if not wav_path.exists():
            return {
                "ok": False,
                "stage": stage,
                "error": "capture_wav_missing",
                "transcript": "",
            }
        url = f"{self.ctx.base_url}/v1/transcribe"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=max(45.0, self.ctx.timeout_s)) as client:
                with wav_path.open("rb") as fh:
                    response = client.post(
                        url,
                        headers=headers,
                        files={"audio": (wav_path.name, fh, "audio/wav")},
                    )
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            try:
                payload: dict[str, Any] | str = response.json()
            except Exception:
                payload = response.text[:1000]
            transcript = self._extract_transcript(payload)
            return {
                "ok": response.status_code == 200 and bool(transcript.strip()),
                "stage": stage,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                "transcript": transcript,
                "response": payload,
            }
        except Exception as exc:
            return {"ok": False, "stage": stage, "error": repr(exc), "transcript": ""}

    def _silence_wav(self, *, duration_s: float) -> Path:
        path = self.ctx.report_dir / "voice_oracle_silence.wav"
        rate = 16_000
        frames = np.zeros(max(1, int(rate * duration_s)), dtype=np.int16)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(frames.tobytes())
        return path

    def _negative_wavs(self) -> list[Path]:
        explicit = self._resolve_existing_paths(self.args.negative_wav)
        if explicit:
            return explicit
        base = self.ctx.root / "violawake_data" / "eval_real" / "negatives" / "adversarial"
        if not base.exists():
            return []
        preferred = sorted(base.glob("*violet*.wav")) + sorted(base.glob("*violin*.wav")) + sorted(base.glob("*.wav"))
        return preferred[: max(1, int(self.args.max_negative_scan))]

    def _music_negative_wavs(self) -> list[Path]:
        explicit = self._resolve_existing_paths(self.args.music_negative_wav)
        if explicit:
            return explicit
        candidates = [
            self.ctx.root / "violawake_data" / "eval_real" / "negatives" / "music",
            self.ctx.root / "violawake_data" / "negatives" / "music",
        ]
        paths: list[Path] = []
        for base in candidates:
            if base.exists():
                paths.extend(sorted(base.rglob("*.wav")))
        return paths[: max(1, int(self.args.max_negative_scan))]

    def _resolve_existing_paths(self, values: list[str] | None) -> list[Path]:
        paths: list[Path] = []
        for value in values or []:
            path = Path(value)
            if not path.is_absolute():
                path = self.ctx.root / path
            if path.exists():
                paths.append(path)
        return paths

    @staticmethod
    def _wake_policy_snapshot(text: str) -> dict[str, Any]:
        return evaluate_wake_policy(text).to_dict()

    @staticmethod
    def _tail_text(path: Path, max_chars: int) -> str:
        data = path.read_bytes()
        if len(data) > max_chars:
            data = data[-max_chars:]
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _read_from_offset(path: Path | None, offset: int) -> str:
        if path is None or not path.exists():
            return ""
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", errors="replace")

    @staticmethod
    def _matching_lines(text: str, patterns: tuple[str, ...]) -> list[str]:
        return [
            Oracle._shorten_line(line.strip())
            for line in text.splitlines()
            if any(pattern in line for pattern in patterns)
        ]

    @staticmethod
    def _shorten_line(line: str, max_chars: int = 420) -> str:
        if len(line) <= max_chars:
            return line
        return f"{line[:max_chars]} ... [truncated {len(line) - max_chars} chars]"

    def _wait_for_patterns(
        self,
        path: Path,
        offset: int,
        patterns: tuple[str, ...],
        timeout_s: float,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            text = self._read_from_offset(path, offset)
            for pattern in patterns:
                if pattern in text:
                    return {
                        "matched": True,
                        "pattern": pattern,
                        "elapsed_s": round(timeout_s - (deadline - time.time()), 3),
                    }
            time.sleep(0.2)
        return {"matched": False, "patterns": list(patterns), "timeout_s": timeout_s}

    def _find_device(self, query: str | None, *, input: bool = False, output: bool = False) -> dict[str, Any] | None:
        if not query:
            return None
        lower = query.lower()
        for device in self.ctx.devices:
            if input and device["in"] <= 0:
                continue
            if output and device["out"] <= 0:
                continue
            name = str(device["name"]).lower()
            if lower in name or name in lower:
                return device
        return None

    def _wake_wavs(self) -> list[Path]:
        if self.args.wake_wav:
            path = Path(self.args.wake_wav)
            if not path.is_absolute():
                path = self.ctx.root / path
            return [path] if path.exists() else []
        candidates = [
            self.ctx.root / "violawake_data" / "eval_real" / "positives",
            self.ctx.root / "violawake_data" / "positives" / "real",
            self.ctx.root.parent / "NOVVIOLA" / "violawake_data" / "eval_real" / "positives",
            self.ctx.root.parent / "NOVVIOLA" / "violawake_data" / "positives" / "real",
        ]
        paths: list[Path] = []
        for base in candidates:
            if base.exists():
                paths.extend(sorted(base.rglob("*.wav")))
        return paths

    def _score_wake_audio(self, wav_path: Path) -> float:
        from violawake import ViolaWake

        model = self.ctx.root / "violawake_data" / "trained_models" / "temporal_cnn.onnx"
        engine = ViolaWake(
            model_path=str(model),
            threshold=float(self.args.wake_threshold),
            debounce_seconds=0.0,
        )
        audio = self._read_wav_float32(wav_path, target_rate=16_000)
        if len(audio) <= 24_000:
            return float(engine.process_audio(audio))
        scores: list[float] = []
        step = 1_600
        for start in range(0, max(1, len(audio) - 24_000 + 1), step):
            scores.append(float(engine.process_audio(audio[start : start + 24_000])))
        return max(scores) if scores else 0.0

    def _command_wav(self) -> Path | None:
        if self.args.command_wav:
            path = Path(self.args.command_wav)
            if not path.is_absolute():
                path = self.ctx.root / path
            return path if path.exists() else None
        if bool(self.args.auto_command_wav):
            return self._generated_command_wav()
        self.results.append(
            StageResult(
                "command_wav_generation",
                "fail",
                details={
                    "missing_capability": "real user command WAV is required",
                    "reason": "synthetic pyttsx3 self-speak cannot prove real mic speech",
                },
            )
        )
        return None

    def _generated_command_wav(self) -> Path | None:
        if self.ctx.generated_command_wav and self.ctx.generated_command_wav.exists():
            return self.ctx.generated_command_wav
        out_path = self.ctx.report_dir / "auto_command_stimulus.wav"
        errors: list[str] = []
        text = str(self.args.command_text or DEFAULT_COMMAND_TEXT)
        try:
            from config.settings import settings as app_settings
            from voice.synthesis.factory import create_kokoro

            engine = create_kokoro(app_settings)
            if engine is not None:
                pcm = asyncio.run(engine.synthesize(text))
                if pcm:
                    rate = int(getattr(engine, "last_sample_rate", 24_000) or 24_000)
                    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    self._write_wav_float32(out_path, samples, rate)
                    self.ctx.generated_command_wav = out_path
                    self.ctx.command_stimulus = {
                        "source": "kokoro_local_generated_audio_stimulus",
                        "text": text,
                        "path": str(out_path),
                        "sample_rate": rate,
                        "bytes": len(pcm),
                    }
                    return out_path
                errors.append("kokoro returned empty PCM")
            else:
                errors.append("kokoro unavailable")
        except Exception as exc:
            errors.append("kokoro generation failed: %r" % (exc,))

        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.save_to_file(text, str(out_path))
            engine.runAndWait()
            if out_path.exists() and out_path.stat().st_size > 0:
                self.ctx.generated_command_wav = out_path
                self.ctx.command_stimulus = {
                    "source": "pyttsx3_generated_audio_stimulus",
                    "text": text,
                    "path": str(out_path),
                    "errors": errors,
                }
                return out_path
            errors.append("pyttsx3 produced no WAV")
        except Exception as exc:
            errors.append("pyttsx3 generation failed: %r" % (exc,))
        self.ctx.command_stimulus = {
            "source": "auto_generation_failed",
            "text": text,
            "errors": errors,
        }
        self.results.append(
            StageResult(
                "command_wav_generation",
                "fail",
                details={
                    "missing_capability": "could not generate automated command audio stimulus",
                    "errors": errors,
                },
            )
        )
        return None

    @staticmethod
    def _extract_transcript(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return str(data.get("transcript") or data.get("text") or "")

    @staticmethod
    def _extract_error_code(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or "")
        if isinstance(error, str):
            return error
        if isinstance(payload.get("code"), str):
            return str(payload["code"])
        return ""

    @staticmethod
    def _read_wav_float32(path: Path, target_rate: int | None = None) -> np.ndarray:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            width = wav.getsampwidth()
            frames = wav.readframes(wav.getnframes())
        if width == 2:
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 4:
            samples = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"unsupported sample width: {width}")
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if target_rate and rate != target_rate:
            duration = len(samples) / float(rate)
            old_x = np.linspace(0.0, duration, num=len(samples), endpoint=False)
            new_len = round(duration * target_rate)
            new_x = np.linspace(0.0, duration, num=new_len, endpoint=False)
            samples = np.interp(new_x, old_x, samples).astype(np.float32)
        return samples.astype(np.float32)

    @staticmethod
    def _play_wav(path: Path, device_index: int, *, gain: float = 1.0) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice unavailable") from exc
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            width = wav.getsampwidth()
            frames = wav.readframes(wav.getnframes())
        if width == 2:
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 4:
            data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"unsupported sample width: {width}")
        if channels > 1:
            data = data.reshape(-1, channels)
        data = np.clip(data * float(gain), -1.0, 1.0)
        sd.play(data, samplerate=rate, device=device_index)
        sd.wait()

    def _playrec_wav(
        self,
        path: Path,
        *,
        input_index: int,
        output_index: int,
        out_path: Path,
        gain: float,
    ) -> dict[str, Any]:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice unavailable") from exc
        input_device = self.ctx.devices[input_index]
        output_device = self.ctx.devices[output_index]
        rate = int(input_device.get("sample_rate") or output_device.get("sample_rate") or 44_100)
        samples = self._read_wav_float32(path, target_rate=rate)
        samples = np.clip(samples * float(gain), -1.0, 1.0)
        pre = np.zeros(int(0.25 * rate), dtype=np.float32)
        post = np.zeros(int(0.75 * rate), dtype=np.float32)
        mono = np.concatenate([pre, samples, post])
        out_channels = max(1, min(2, int(output_device.get("out", 1) or 1)))
        playback = np.tile(mono.reshape(-1, 1), (1, out_channels))
        recording = sd.playrec(
            playback,
            samplerate=rate,
            channels=1,
            dtype="float32",
            device=(input_index, output_index),
            blocking=True,
        )
        captured = np.asarray(recording, dtype=np.float32).reshape(-1)
        self._write_wav_float32(out_path, captured, rate)
        rms_float = float(math.sqrt(float(np.mean(captured * captured)))) if captured.size else 0.0
        peak_float = float(np.max(np.abs(captured))) if captured.size else 0.0
        return {
            "path": str(out_path),
            "rate": rate,
            "frames": int(captured.size),
            "rms": rms_float * 32768.0,
            "peak": peak_float * 32768.0,
            "rms_float": rms_float,
            "peak_float": peak_float,
        }

    @staticmethod
    def _write_wav_float32(path: Path, samples: np.ndarray, rate: int) -> None:
        clipped = np.clip(samples, -1.0, 1.0)
        int16 = (clipped * 32767.0).astype(np.int16)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(int16.tobytes())

    @staticmethod
    def _pcm_rms_int16(pcm: bytes | None) -> float:
        if not pcm:
            return 0.0
        usable = len(pcm) - (len(pcm) % 2)
        if usable <= 0:
            return 0.0
        samples = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return 0.0
        return float(math.sqrt(float(np.mean(samples * samples))))

    def _capture_loopback_during(
        self,
        fn: Callable[[], None],
        out_path: Path,
        *,
        duration_s: float,
    ) -> dict[str, Any]:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            fn()
            return {"status": "skip", "reason": "pyaudiowpatch unavailable"}

        # Long-lived capture: only the init/terminate moments are serialized under
        # the process-wide lock (holding it across the multi-second capture would
        # stall unrelated audio). pyaudiowpatch precludes portaudio_instance().
        with PORTAUDIO_LOCK:
            pa = pyaudio.PyAudio()
        try:
            loopback_index = None
            loopback_name = None
            preferred_output = str((self.ctx.settings or {}).get("output_device", "") or "").lower()
            for index in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(index)
                name = str(info.get("name", ""))
                if info.get("isLoopbackDevice") and preferred_output and preferred_output in name.lower():
                    loopback_index = index
                    loopback_name = name
                    break
            if loopback_index is None:
                for index in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(index)
                    if info.get("isLoopbackDevice"):
                        loopback_index = index
                        loopback_name = info.get("name")
                        break
            if loopback_index is None:
                fn()
                return {"status": "skip", "reason": "no WASAPI loopback device exposed"}

            info = pa.get_device_info_by_index(loopback_index)
            rate = int(info.get("defaultSampleRate", 48_000))
            channels = max(1, min(2, int(info.get("maxInputChannels", 2) or 2)))
            frames: list[bytes] = []
            fn_errors: list[str] = []
            fn_done = threading.Event()

            def run_fn() -> None:
                try:
                    fn()
                except Exception as exc:
                    fn_errors.append(repr(exc))
                finally:
                    fn_done.set()

            stream = open_stream(
                pa,
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=loopback_index,
                frames_per_buffer=1024,
            )
            fn_thread = threading.Thread(target=run_fn, name="voice-oracle-tts-output", daemon=True)
            started = time.time()
            min_end = started + duration_s
            max_end = started + max(duration_s + 15.0, float(self.args.loopback_max_capture_s))
            try:
                fn_thread.start()
                while time.time() < max_end:
                    frames.append(stream.read(1024, exception_on_overflow=False))
                    if fn_done.is_set() and time.time() >= min_end:
                        break
            finally:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                with contextlib.suppress(Exception):
                    stream.close()
                fn_thread.join(timeout=2.0)
            raw = b"".join(frames)
            with wave.open(str(out_path), "wb") as wav:
                wav.setnchannels(channels)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(raw)
            rms = self._pcm_rms_int16(raw)
            return {
                "status": "captured",
                "path": str(out_path),
                "bytes": len(raw),
                "rms": rms,
                "fn_errors": fn_errors,
                "capture_elapsed_s": round(time.time() - started, 3),
                "device": {
                    "index": loopback_index,
                    "name": loopback_name,
                    "sample_rate": rate,
                    "channels": channels,
                },
            }
        finally:
            with contextlib.suppress(Exception):
                # Drains guarded streams before Pa_Terminate (#4650).
                terminate_portaudio(pa)

    def _capture_loopback_window(
        self,
        out_path: Path,
        *,
        duration_s: float,
        preferred_output: dict[str, Any] | None = None,
        exclude_names: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            return {
                "status": "skip",
                "reason": "pyaudiowpatch unavailable",
                "path": str(out_path),
            }

        # Long-lived capture: only init/terminate are serialized under the lock.
        with PORTAUDIO_LOCK:
            pa = pyaudio.PyAudio()
        try:
            selected = self._select_loopback_device(
                pa, preferred_output=preferred_output, exclude_names=exclude_names or []
            )
            if selected is None:
                return {
                    "status": "skip",
                    "reason": "no matching WASAPI loopback device exposed",
                    "path": str(out_path),
                }
            loopback_index, loopback_name, info = selected
            rate = int(info.get("defaultSampleRate", 48_000))
            channels = max(1, min(2, int(info.get("maxInputChannels", 2) or 2)))
            frames: list[bytes] = []
            stream = open_stream(
                pa,
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=loopback_index,
                frames_per_buffer=1024,
            )
            started = time.time()
            try:
                deadline = started + max(0.1, float(duration_s))
                while time.time() < deadline:
                    frames.append(stream.read(1024, exception_on_overflow=False))
            finally:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                with contextlib.suppress(Exception):
                    stream.close()
            raw = b"".join(frames)
            with wave.open(str(out_path), "wb") as wav:
                wav.setnchannels(channels)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(raw)
            rms = self._pcm_rms_int16(raw)
            return {
                "status": "captured",
                "path": str(out_path),
                "bytes": len(raw),
                "rms": rms,
                "capture_started_at": started,
                "capture_elapsed_s": round(time.time() - started, 3),
                "device": {
                    "index": loopback_index,
                    "name": loopback_name,
                    "sample_rate": rate,
                    "channels": channels,
                },
            }
        finally:
            with contextlib.suppress(Exception):
                # Drains guarded streams before Pa_Terminate (#4650).
                terminate_portaudio(pa)

    def _select_loopback_device(
        self,
        pa: Any,
        *,
        preferred_output: dict[str, Any] | None = None,
        exclude_names: list[str],
    ) -> tuple[int, str, dict[str, Any]] | None:
        preferred_names = []
        explicit = str(self.args.tts_loopback_output or "").strip()
        if explicit:
            preferred_names.append(explicit.lower())
        if preferred_output:
            preferred_names.append(str(preferred_output.get("name", "")).lower())
        configured = str((self.ctx.settings or {}).get("output_device", "") or "").lower()
        if configured:
            preferred_names.append(configured)
        excluded = [name.lower() for name in exclude_names if name]
        loopbacks: list[tuple[int, str, dict[str, Any]]] = []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            if not info.get("isLoopbackDevice"):
                continue
            name = str(info.get("name", ""))
            lower = name.lower()
            if any(excluded_name and excluded_name in lower for excluded_name in excluded):
                continue
            loopbacks.append((index, name, info))
        for preferred in preferred_names:
            if not preferred:
                continue
            for item in loopbacks:
                lower = item[1].lower()
                if preferred in lower or lower.replace(" [loopback]", "") in preferred:
                    return item
        return loopbacks[0] if loopbacks else None

    def _write_reports(self) -> None:
        completed_at = time.time()
        gap_list = [
            {
                "stage": result.name,
                "status": result.status,
                "details": result.details,
                "evidence": result.evidence,
                "trace_ids": result.trace_ids,
            }
            for result in self.results
            if result.status not in ("pass", "skip")
        ]
        skipped_surface_inventory = [
            {
                "stage": result.name,
                "missing_capability": result.details.get("missing_capability"),
                "details": result.details,
            }
            for result in self.results
            if result.details.get("missing_capability") or result.status == "skip"
        ]
        trace_ids = sorted(
            {trace_id for result in self.results for trace_id in result.trace_ids} | set(self.ctx.trace_ids)
        )
        payload = {
            "run_id": self.ctx.run_id,
            "started_at": self.ctx.started_at,
            "started_at_iso": datetime.fromtimestamp(self.ctx.started_at, tz=UTC).isoformat(),
            "completed_at": completed_at,
            "completed_at_iso": datetime.fromtimestamp(completed_at, tz=UTC).isoformat(),
            "overall_status": "fail" if self._has_required_failure() else "pass",
            "base_url": self.ctx.base_url,
            "root": str(self.ctx.root),
            "trace_ids": trace_ids,
            "command_trace_report": (str(self.ctx.command_trace_report) if self.ctx.command_trace_report else None),
            "gap_list": gap_list,
            "skipped_surface_inventory": skipped_surface_inventory,
            "audio_route": {
                "candidates": self.ctx.audio_route_candidates,
                "selected": self.ctx.live_audio_route,
                "restore": self.ctx.audio_route_restore,
            },
            "runtime_settings_restore": self.ctx.runtime_settings_restore,
            "command_stimulus": self.ctx.command_stimulus,
            "ducking_output_level": self.ctx.ducking_output_level,
            "settings": {
                "voice_mode": self.ctx.settings.get("voice_mode"),
                "wake_word_engine": self.ctx.settings.get("wake_word_engine"),
                "stt_engine": self.ctx.settings.get("stt_engine"),
                "whisper_model": self.ctx.settings.get("whisper_model"),
                "tts_enabled": self.ctx.settings.get("tts_enabled"),
                "ai_source": self.ctx.settings.get("ai_source"),
                "llm_model": self.ctx.settings.get("llm_model"),
                "input_device": self.ctx.settings.get("input_device"),
                "output_device": self.ctx.settings.get("output_device"),
            },
            "results": [asdict(result) for result in self.results],
        }
        json_path = self.ctx.report_dir / ("voice_oracle_%s.json" % self.ctx.run_id)
        latest_json_path = self.ctx.report_dir / "voice_oracle_latest.json"
        json_text = json.dumps(payload, indent=2, sort_keys=True, default=str, ensure_ascii=False)
        json_path.write_text(json_text, encoding="utf-8")
        latest_json_path.write_text(json_text, encoding="utf-8")

        lines = [
            "# L2 Voice Oracle Run",
            "",
            f"- Run ID: `{self.ctx.run_id}`",
            f"- Overall: `{payload['overall_status']}`",
            f"- Root: `{self.ctx.root}`",
            f"- Base URL: `{self.ctx.base_url}`",
            f"- Log: `{self.ctx.log_path}`",
            f"- Trace IDs: `{', '.join(trace_ids) if trace_ids else '<none>'}`",
            "",
            "## Stages",
            "",
        ]
        for result in self.results:
            latency = "" if result.latency_ms is None else f" ({result.latency_ms:.1f} ms)"
            lines.append(f"- **{result.status.upper()}** `{result.name}`{latency}")
            if result.evidence:
                for evidence in result.evidence:
                    lines.append(f"  - {evidence}")
        lines.extend(["", "## Complete Gap List", ""])
        if gap_list:
            for gap in gap_list:
                lines.append(f"- `{gap['stage']}` status=`{gap['status']}`")
                for evidence in gap.get("evidence") or []:
                    lines.append(f"  - {evidence}")
        else:
            lines.append("- none")
        lines.extend(["", "## Skipped Surface Inventory", ""])
        if skipped_surface_inventory:
            for item in skipped_surface_inventory:
                lines.append(f"- `{item['stage']}`: {item.get('missing_capability') or 'status=skip'}")
        else:
            lines.append("- none")
        lines.extend(["", f"JSON: `{json_path}`", ""])
        md_path = self.ctx.report_dir / ("voice_oracle_%s.md" % self.ctx.run_id)
        latest_md_path = self.ctx.report_dir / "voice_oracle_latest.md"
        md_text = "\n".join(lines)
        md_path.write_text(md_text, encoding="utf-8")
        latest_md_path.write_text(md_text, encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the instrumented live oracle for the Viola voice lane.")
    parser.add_argument(
        "--root",
        default=os.getcwd(),
        help="Project root containing .viola/settings.json.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Running Viola backend URL.")
    parser.add_argument("--api-key-file", default=None, help="Optional explicit API key file.")
    parser.add_argument(
        "--report-dir",
        default=str(DEFAULT_REPORT_DIR),
        help="Directory for JSON/markdown/audio evidence.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Default HTTP timeout in seconds.")
    parser.add_argument(
        "--log-fresh-wait-s",
        type=float,
        default=6.0,
        help="Seconds to wait for fresh post-start log evidence.",
    )
    parser.add_argument(
        "--settings-apply-wait-s",
        type=float,
        default=4.0,
        help="Seconds to wait for runtime settings changes to persist before proof stages.",
    )
    parser.add_argument(
        "--wake-wav",
        default=None,
        help="Known wake-positive WAV. Defaults to local violawake positives.",
    )
    parser.add_argument(
        "--wake-threshold",
        type=float,
        default=0.90,
        help="Wake score threshold to enforce.",
    )
    parser.add_argument(
        "--max-wake-scan",
        type=int,
        default=20,
        help="Number of wake-positive WAVs to scan.",
    )
    parser.add_argument(
        "--command-text",
        default=DEFAULT_COMMAND_TEXT,
        help="Text for the command audio and intent probe.",
    )
    parser.add_argument(
        "--command-wav",
        default=None,
        help="Required real-user command WAV for STT/live injection.",
    )
    parser.add_argument(
        "--auto-command-wav",
        action="store_true",
        help="Generate a local speech command stimulus, then verify it through real Whisper.",
    )
    parser.add_argument(
        "--command-tts-rate",
        type=int,
        default=135,
        help="Deprecated; synthetic command generation is forbidden.",
    )
    parser.add_argument(
        "--intent-baseline-volume",
        type=int,
        default=20,
        help="Known starting volume for side-effect checks.",
    )
    parser.add_argument(
        "--intent-timeout-s",
        type=float,
        default=20.0,
        help="Seconds to wait for /v1/command.",
    )
    parser.add_argument(
        "--expected-transcript-terms",
        nargs="*",
        default=None,
        help="Terms expected in the live STT transcript.",
    )
    parser.add_argument(
        "--expected-tts-terms",
        nargs="*",
        default=None,
        help="Terms expected when transcribing the running voice stack TTS output.",
    )
    parser.add_argument("--tts-text", default=DEFAULT_TTS_TEXT, help="Text used for real TTS probes.")
    parser.add_argument(
        "--min-tts-rms",
        type=float,
        default=50.0,
        help="Minimum int16 RMS for synthesized TTS PCM.",
    )
    parser.add_argument(
        "--loopback-capture-s",
        type=float,
        default=5.0,
        help="Seconds to capture output loopback during TTS.",
    )
    parser.add_argument(
        "--loopback-max-capture-s",
        type=float,
        default=20.0,
        help="Maximum seconds for loopback capture.",
    )
    parser.add_argument(
        "--min-loopback-rms",
        type=float,
        default=DEFAULT_AUDIBLE_LOOPBACK_RMS,
        help="Minimum int16 RMS for audible loopback capture.",
    )
    parser.add_argument(
        "--inject-live",
        action="store_true",
        help="Compatibility flag; live injection is always required.",
    )
    parser.add_argument(
        "--virtual-output",
        default="MOTIV Mix Virtual Input",
        help="Output endpoint feeding virtual mic input.",
    )
    parser.add_argument(
        "--virtual-input",
        default="MOTIV Mix Virtual Output",
        help="Input endpoint Viola should capture.",
    )
    parser.add_argument(
        "--injection-gain",
        type=float,
        default=6.0,
        help="Gain applied to wake WAV during live injection.",
    )
    parser.add_argument(
        "--calibration-gain",
        action="append",
        type=float,
        default=[],
        help="Extra gain candidate for wake injection calibration; may be repeated.",
    )
    parser.add_argument(
        "--max-calibration-wavs",
        type=int,
        default=3,
        help="Maximum wake-positive WAVs to try during route/gain calibration.",
    )
    parser.add_argument(
        "--max-calibration-attempts",
        type=int,
        default=24,
        help="Maximum bounded route/gain/wake calibration attempts.",
    )
    parser.add_argument(
        "--max-acoustic-routes",
        type=int,
        default=4,
        help="Maximum automated speaker-to-mic fallback routes to test when virtual routing is silent.",
    )
    parser.add_argument(
        "--allow-acoustic-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow automated speaker-to-mic routing when virtual loopback endpoints do not carry audio.",
    )
    parser.add_argument(
        "--command-injection-gain",
        type=float,
        default=0.0,
        help="Gain applied to command WAV after wake; 0 inherits the calibrated wake gain.",
    )
    parser.add_argument(
        "--negative-injection-gain",
        type=float,
        default=1.0,
        help="Gain applied to negative WAVs.",
    )
    parser.add_argument(
        "--negative-wav",
        action="append",
        default=[],
        help="Confusable negative WAV; may be repeated.",
    )
    parser.add_argument(
        "--music-negative-wav",
        action="append",
        default=[],
        help="Music negative WAV; may be repeated.",
    )
    parser.add_argument(
        "--max-negative-scan",
        type=int,
        default=10,
        help="Number of negative WAVs to scan if defaults exist.",
    )
    parser.add_argument(
        "--min-injection-rms",
        type=float,
        default=DEFAULT_MIN_INJECTION_RMS,
        help="Minimum mic RMS for injection calibration.",
    )
    parser.add_argument(
        "--max-injection-peak",
        type=float,
        default=30000.0,
        help="Maximum accepted captured peak during calibrated injection.",
    )
    parser.add_argument(
        "--wake-wait-s",
        type=float,
        default=12.0,
        help="Seconds to wait for wake acceptance.",
    )
    parser.add_argument(
        "--negative-wait-s",
        type=float,
        default=5.0,
        help="Seconds to watch logs after negative audio.",
    )
    parser.add_argument(
        "--command-delay-s",
        type=float,
        default=0.4,
        help="Delay after wake acceptance before command audio.",
    )
    parser.add_argument(
        "--roundtrip-wait-s",
        type=float,
        default=45.0,
        help="Seconds to wait for STT/TTS after injection.",
    )
    parser.add_argument(
        "--route-restart-wait-s",
        type=float,
        default=4.0,
        help="Seconds to wait after applying the calibrated runtime input/output route.",
    )
    parser.add_argument(
        "--no-restore-audio-route",
        action="store_true",
        help="Leave the calibrated audio route in runtime settings after the run.",
    )
    parser.add_argument(
        "--tts-capture-window-s",
        type=float,
        default=5.0,
        help="Seconds to capture output loopback after command injection has ended.",
    )
    parser.add_argument(
        "--tts-capture-start-delay-s",
        type=float,
        default=0.15,
        help="Delay between command injection ending and TTS loopback capture start.",
    )
    parser.add_argument(
        "--tts-loopback-output",
        default=None,
        help="Specific playback endpoint whose WASAPI loopback should be used for Viola TTS capture.",
    )
    parser.add_argument(
        "--duck-sample-interval-s",
        type=float,
        default=0.25,
        help="Seconds between player volume samples during the live voice turn.",
    )
    parser.add_argument(
        "--min-duck-attenuation-delta",
        type=float,
        default=5.0,
        help="Minimum observed volume drop required for ducking proof.",
    )
    parser.add_argument(
        "--duck-restore-tolerance",
        type=float,
        default=5.0,
        help="Allowed difference from baseline/post-command volume for restore proof.",
    )
    parser.add_argument(
        "--duck-baseline-volume",
        type=int,
        default=60,
        help="Player volume to set while proving real output-level ducking.",
    )
    parser.add_argument(
        "--ducking-play-query",
        default=DEFAULT_DUCKING_PLAY_QUERY,
        help="Local playback query the oracle can start when ducking proof needs active music.",
    )
    parser.add_argument(
        "--ducking-play-source",
        default="local",
        help="Playback source for oracle-started ducking music.",
    )
    parser.add_argument(
        "--ducking-restart-playback",
        action="store_true",
        help="Stop existing playback and start the ducking proof track after the calibrated route is applied.",
    )
    parser.add_argument(
        "--ducking-start-timeout-s",
        type=float,
        default=20.0,
        help="Seconds to wait for oracle-started ducking playback to become active.",
    )
    parser.add_argument(
        "--duck-output-window-s",
        type=float,
        default=1.5,
        help="Seconds of output loopback to sample for each ducking level.",
    )
    parser.add_argument(
        "--duck-settle-s",
        type=float,
        default=0.35,
        help="Seconds to wait after duck/unduck before sampling output loopback.",
    )
    parser.add_argument(
        "--min-music-output-rms",
        type=float,
        default=40.0,
        help="Minimum baseline output RMS proving music is genuinely audible.",
    )
    parser.add_argument(
        "--max-ducked-output-ratio",
        type=float,
        default=0.75,
        help="Maximum ducked/baseline RMS ratio accepted for attenuation proof.",
    )
    parser.add_argument(
        "--min-restored-output-ratio",
        type=float,
        default=0.8,
        help="Minimum restored/baseline RMS ratio accepted for restore proof.",
    )
    parser.add_argument(
        "--trace-user-id",
        default=DEFAULT_TRACE_READ_USER,
        help="User id for TraceReader; default resolves the logged-in desktop user.",
    )
    parser.add_argument(
        "--recovery-timeout-s",
        type=float,
        default=12.0,
        help="Timeout for recovery/no-hang probes.",
    )
    args = parser.parse_args(argv)
    if args.expected_transcript_terms is None:
        args.expected_transcript_terms = expected_command_terms(args.command_text, None)["terms"]
    if args.expected_tts_terms is None:
        args.expected_tts_terms = expected_command_terms(args.command_text, None)["terms"]
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return Oracle(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
