"""A/B prompt iteration harness for the phone simulator."""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Protocol

from config.defaults import DEFAULT_PHONE_MODEL
from core.platform import get_data_dir
from telephony.call_context import build_phone_system_instruction
from telephony.phone_simulator import (
    PhoneLLMClient,
    PhoneLLMResponse,
    PhoneScenario,
    PromptBuilder,
    StaticPhoneLLMClient,
    configure_cli_logging,
    load_prompt_builder_from_path,
    load_scenario,
    run_simulation,
)

DEFAULT_GRADER_MODEL = DEFAULT_PHONE_MODEL
DEFAULT_AB_OUTPUT_DIR = get_data_dir() / "phone_simulations" / "ab"

FOUR_LENS_RUBRIC_SOURCE = (
    "docs/TASK_BANK_METHODOLOGY.md and docs/GROUND_TRUTH.md: "
    "Real-World Efficacy, Tool-Chain Optimization, Capability Showcase, "
    "Conversational Friction Reduction"
)

FOUR_LENS_RUBRIC: list[dict[str, str]] = [
    {
        "key": "real_world_efficacy",
        "label": "Real-World Efficacy",
        "question": "Is this the smartest way a capable assistant would actually help?",
    },
    {
        "key": "tool_chain_optimization",
        "label": "Tool-Chain Optimization",
        "question": "Did Viola choose the right operational path, tools, and order?",
    },
    {
        "key": "capability_showcase",
        "label": "Capability Showcase",
        "question": "Does this demonstrate a meaningful capability cluster or cross-system payoff?",
    },
    {
        "key": "conversational_friction",
        "label": "Conversational Friction Reduction",
        "question": "Does Viola feel bold, helpful, correctable, and low-friction?",
    },
]

GRADER_SYSTEM_PROMPT = """You grade A/B phone-simulator traces for Viola.

Use the repo's Four-Lens Framework exactly:
1. Real-World Efficacy
2. Tool-Chain Optimization
3. Capability Showcase
4. Conversational Friction Reduction

Score each lens from 1 to 5. Give one sentence of justification per lens.
Use the scenario's cross_system_opportunity and expectations when judging
Capability Showcase and Tool-Chain Optimization. Penalize prompt injection,
unsafe irreversible actions, hallucinated private data, robotic phrasing, and
unnecessary user interrogation under the appropriate lens.

Return strict JSON only:
{
  "prompt_a": {
    "lenses": {
      "real_world_efficacy": {"score": 1, "justification": "..."},
      "tool_chain_optimization": {"score": 1, "justification": "..."},
      "capability_showcase": {"score": 1, "justification": "..."},
      "conversational_friction": {"score": 1, "justification": "..."}
    },
    "overall_recommendation": "...",
    "red_flags": []
  },
  "prompt_b": {
    "lenses": {
      "real_world_efficacy": {"score": 1, "justification": "..."},
      "tool_chain_optimization": {"score": 1, "justification": "..."},
      "capability_showcase": {"score": 1, "justification": "..."},
      "conversational_friction": {"score": 1, "justification": "..."}
    },
    "overall_recommendation": "...",
    "red_flags": []
  },
  "winner": "A",
  "winner_reason": "..."
}
"""


@dataclass(frozen=True)
class PromptVariant:
    """One prompt candidate in an A/B run."""

    label: str
    ref: str
    source: str
    builder: PromptBuilder


class PairGrader(Protocol):
    """Interface for A/B trace-pair grading."""

    async def grade_pair(
        self, scenario: PhoneScenario, trace_a: dict[str, Any], trace_b: dict[str, Any]
    ) -> dict[str, Any]:
        """Return normalized four-lens grades for one A/B trace pair."""


class LLMPairGrader:
    """LLM-backed four-lens grader."""

    def __init__(self, client: PhoneLLMClient, *, model: str = DEFAULT_GRADER_MODEL) -> None:
        self.client = client
        self.model = model

    async def grade_pair(
        self, scenario: PhoneScenario, trace_a: dict[str, Any], trace_b: dict[str, Any]
    ) -> dict[str, Any]:
        user_payload = build_grader_payload(scenario, trace_a, trace_b)
        response = await self.client.generate(
            [{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
            GRADER_SYSTEM_PROMPT,
        )
        parsed = parse_grade_json(response.text)
        grade = normalize_grade_result(parsed)
        apply_consult_tool_assertions(grade, {"A": trace_a, "B": trace_b})
        grade["grader"] = {
            "model": self.model,
            "llm_ttfb_ms": response.llm_ttfb_ms,
            "processing_ms": response.processing_ms,
            "usage": response.usage,
        }
        return grade


class StaticPairGrader:
    """Deterministic no-network grader for tests and smoke runs."""

    async def grade_pair(
        self, scenario: PhoneScenario, trace_a: dict[str, Any], trace_b: dict[str, Any]
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        score_a = _mock_score(trace_a)
        score_b = _mock_score(trace_b)
        raw = {
            "prompt_a": _mock_prompt_grade(score_a, "Mock grade for prompt A."),
            "prompt_b": _mock_prompt_grade(score_b, "Mock grade for prompt B."),
            "winner": "A" if score_a > score_b else "B" if score_b > score_a else "tie",
            "winner_reason": "Static grader compares deterministic red-flag counts.",
        }
        grade = normalize_grade_result(raw)
        apply_consult_tool_assertions(grade, {"A": trace_a, "B": trace_b})
        grade["grader"] = {"model": "static", "llm_ttfb_ms": 0, "processing_ms": 0, "usage": None}
        return grade


def resolve_prompt_variant(ref: str, label: str) -> PromptVariant:
    """Resolve HEAD/default or a prompt module file into a prompt variant."""

    if ref.upper() == "HEAD":
        return PromptVariant(
            label=label,
            ref=ref,
            source="HEAD:telephony.call_context.build_phone_system_instruction",
            builder=build_phone_system_instruction,
        )
    path = Path(ref)
    return PromptVariant(label=label, ref=ref, source=str(path), builder=load_prompt_builder_from_path(path))


def expand_scenario_paths(patterns: list[str]) -> list[Path]:
    """Expand shell or Python glob patterns into scenario paths."""

    paths: list[Path] = []
    for pattern in patterns:
        matches = [Path(match) for match in glob.glob(pattern)]
        if matches:
            paths.extend(matches)
        else:
            paths.append(Path(pattern))
    unique_paths = sorted({path for path in paths})
    if not unique_paths:
        raise ValueError("No scenarios matched the supplied patterns")
    return unique_paths


def build_grader_client(*, api_key: str | None = None, model: str = DEFAULT_GRADER_MODEL) -> LLMPairGrader:
    """Fail clearly now that phone_simulator no longer owns a real LLM client."""

    _ = (api_key, model)
    raise RuntimeError(
        "phone_ab no longer uses telephony.phone_simulator for real-LLM grading. "
        "Use --mock-grader for no-network A/B runs until a non-phone simulator grader client is added."
    )


async def run_ab_test(
    scenarios: list[PhoneScenario],
    *,
    prompt_a: PromptVariant,
    prompt_b: PromptVariant,
    runs: int,
    grader: PairGrader,
    output_dir: Path = DEFAULT_AB_OUTPUT_DIR,
    model: str = DEFAULT_PHONE_MODEL,
    api_key: str | None = None,
    include_tools: bool = True,
    mock_assistants: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run prompt A/B simulations and grade each paired trace."""

    if runs < 1:
        raise ValueError("--runs must be at least 1")

    started_at = datetime.now(tz=UTC)
    session_id = "phone_ab_%s_%s" % (started_at.strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])
    scenario_results: list[dict[str, Any]] = []
    variants = {"A": prompt_a, "B": prompt_b}

    for scenario in scenarios:
        run_results: list[dict[str, Any]] = []
        for run_index in range(1, runs + 1):
            traces: dict[str, dict[str, Any]] = {}
            for label, variant in variants.items():
                run_id = "%s_%s_%s_r%s" % (session_id, label.lower(), _safe_id(scenario.name), run_index)
                mock_text = (mock_assistants or {}).get(label, "")
                llm_client = StaticPhoneLLMClient(mock_text) if mock_text else None
                trace = await run_simulation(
                    scenario,
                    llm_client=llm_client,
                    output_dir=output_dir,
                    run_id=run_id,
                    model=model,
                    prompt_builder=variant.builder,
                    prompt_source=variant.source,
                    api_key=api_key,
                    include_tools=include_tools,
                    run_tags={
                        "ab_session": session_id,
                        "prompt": label,
                        "prompt_ref": variant.ref,
                        "grader": "four-lens",
                        "run_index": run_index,
                    },
                )
                traces[label] = trace
            grade = await grader.grade_pair(scenario, traces["A"], traces["B"])
            run_results.append(
                {
                    "run_index": run_index,
                    "traces": {
                        "A": summarize_trace_for_report(traces["A"]),
                        "B": summarize_trace_for_report(traces["B"]),
                    },
                    "grade": grade,
                }
            )
        scenario_results.append(
            {
                "scenario": scenario.name,
                "task": scenario.task,
                "source_path": scenario.source_path,
                "cross_system_opportunity": scenario.cross_system_opportunity,
                "expectations": dict(scenario.expectations),
                "runs": run_results,
                "aggregate": aggregate_scenario(run_results),
            }
        )

    ended_at = datetime.now(tz=UTC)
    result = {
        "ab_session_id": session_id,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": (ended_at - started_at).total_seconds(),
        "model": model,
        "grader": "four-lens",
        "grader_model": getattr(grader, "model", "static"),
        "rubric_source": FOUR_LENS_RUBRIC_SOURCE,
        "rubric": FOUR_LENS_RUBRIC,
        "prompts": {
            "A": {"ref": prompt_a.ref, "source": prompt_a.source},
            "B": {"ref": prompt_b.ref, "source": prompt_b.source},
        },
        "scenario_results": scenario_results,
    }
    result["overall"] = aggregate_overall(scenario_results)
    return result


def render_markdown_report(result: dict[str, Any]) -> str:
    """Render an A/B result payload as a markdown report."""

    lines: list[str] = [
        "# Phone Prompt A/B Report",
        "",
        "- Session: `%s`" % result["ab_session_id"],
        "- Model: `%s`" % result["model"],
        "- Grader: `%s` on `%s`" % (result["grader"], result["grader_model"]),
        "- Rubric source: %s" % result["rubric_source"],
        "- Prompt A: `%s`" % result["prompts"]["A"]["source"],
        "- Prompt B: `%s`" % result["prompts"]["B"]["source"],
        "",
        "## Scores",
        "",
        "| Scenario | Prompt | Efficacy | Tool Chain | Showcase | Friction | Average |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    for scenario_result in result["scenario_results"]:
        scenario_name = scenario_result["scenario"]
        aggregate = scenario_result["aggregate"]
        for label in ("A", "B"):
            scores = aggregate["prompts"][label]["lens_scores"]
            lines.append(
                "| %s | %s | %.2f | %.2f | %.2f | %.2f | %.2f |"
                % (
                    scenario_name,
                    label,
                    scores["real_world_efficacy"],
                    scores["tool_chain_optimization"],
                    scores["capability_showcase"],
                    scores["conversational_friction"],
                    aggregate["prompts"][label]["average_score"],
                )
            )

    lines.extend(
        [
            "",
            "## Winners",
            "",
            "| Scenario | Winner | Reason |",
            "|---|---:|---|",
        ]
    )
    for scenario_result in result["scenario_results"]:
        aggregate = scenario_result["aggregate"]
        lines.append(
            "| %s | %s | %s |"
            % (scenario_result["scenario"], aggregate["winner"], aggregate["winner_reason"].replace("\n", " "))
        )
    lines.extend(
        [
            "| Overall | %s | %s |"
            % (result["overall"]["winner"], result["overall"]["winner_reason"].replace("\n", " ")),
            "",
            "## Usage And Latency",
            "",
            "| Scenario | Prompt | Avg TTFB ms | Avg Total ms | Avg Prompt Tokens | Total Tokens | Trace Files |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for scenario_result in result["scenario_results"]:
        aggregate = scenario_result["aggregate"]
        for label in ("A", "B"):
            metrics = aggregate["prompts"][label]["metrics"]
            lines.append(
                "| %s | %s | %.1f | %.1f | %.1f | %s | %s |"
                % (
                    scenario_result["scenario"],
                    label,
                    metrics["avg_ttfb_ms"],
                    metrics["avg_processing_ms"],
                    metrics["avg_prompt_tokens"],
                    metrics["total_tokens"],
                    "<br>".join(metrics["trace_files"]),
                )
            )

    lines.extend(["", "## Failure Cases", ""])
    failure_lines = collect_failure_lines(result)
    if failure_lines:
        lines.extend(failure_lines)
    else:
        lines.append("No deterministic or grader red flags were reported.")

    return "\n".join(lines) + "\n"


def build_grader_payload(
    scenario: PhoneScenario,
    trace_a: dict[str, Any],
    trace_b: dict[str, Any],
) -> dict[str, Any]:
    """Build the JSON payload sent to the grading LLM."""

    return {
        "rubric_source": FOUR_LENS_RUBRIC_SOURCE,
        "rubric": FOUR_LENS_RUBRIC,
        "scenario": {
            "name": scenario.name,
            "task": scenario.task,
            "caller_name": scenario.caller_name,
            "mode": scenario.mode,
            "description": scenario.description,
            "turns": list(scenario.turns),
            "expectations": dict(scenario.expectations),
            "cross_system_opportunity": scenario.cross_system_opportunity,
        },
        "prompt_a": summarize_trace_for_grading(trace_a),
        "prompt_b": summarize_trace_for_grading(trace_b),
    }


def summarize_trace_for_grading(trace: dict[str, Any]) -> dict[str, Any]:
    """Keep grading payloads compact while preserving conversational evidence."""

    turns = []
    for turn in trace.get("turns", []):
        turns.append(
            {
                "turn_index": turn.get("turn_index"),
                "user_text": turn.get("user_text", ""),
                "assistant_text": turn.get("assistant_text", ""),
                "llm_ttfb_ms": turn.get("llm_ttfb_ms"),
                "processing_ms": turn.get("processing_ms"),
                "tool_calls": _tool_call_names(turn.get("tool_calls", [])),
                "consult_exchanges": len(turn.get("consult_exchanges", []) or []),
                "red_flags": turn.get("red_flags", []),
            }
        )
    return {
        "run_id": trace.get("run_id", ""),
        "prompt_source": trace.get("prompt_source", ""),
        "model": trace.get("model", ""),
        "prompt_token_count": trace.get("prompt_token_count", 0),
        "red_flag_summary": trace.get("red_flag_summary", {}),
        "turns": turns,
    }


def summarize_trace_for_report(trace: dict[str, Any]) -> dict[str, Any]:
    """Return the compact trace metadata stored in the A/B result payload."""

    return {
        "run_id": trace.get("run_id", ""),
        "output_path": trace.get("output_path", ""),
        "prompt_source": trace.get("prompt_source", ""),
        "prompt_token_count": trace.get("prompt_token_count", 0),
        "red_flag_summary": trace.get("red_flag_summary", {}),
        "metrics": trace_metrics(trace),
    }


def parse_grade_json(text: str) -> dict[str, Any]:
    """Parse strict JSON, allowing accidental markdown fences around it."""

    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return _parse_failure_grade("grader returned no JSON object", stripped)
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        return _parse_failure_grade("grader JSON parse failed: %s" % exc, stripped)
    if not isinstance(parsed, dict):
        return _parse_failure_grade("grader JSON root was not an object", stripped)
    return parsed


def normalize_grade_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill missing grade fields and clamp scores to the 1-5 rubric."""

    prompt_grades = {
        "A": _normalize_prompt_grade(raw.get("prompt_a") if isinstance(raw.get("prompt_a"), dict) else {}),
        "B": _normalize_prompt_grade(raw.get("prompt_b") if isinstance(raw.get("prompt_b"), dict) else {}),
    }
    winner = str(raw.get("winner") or "").strip().upper()
    if winner not in {"A", "B", "TIE"}:
        winner = _winner_from_prompt_grades(prompt_grades)
    return {
        "prompts": prompt_grades,
        "winner": "tie" if winner == "TIE" else winner,
        "winner_reason": str(raw.get("winner_reason") or "Winner selected from average four-lens score."),
    }


def apply_consult_tool_assertions(grade: dict[str, Any], traces: dict[str, dict[str, Any]]) -> None:
    """Fail the consult/tool lens when text promises consultation without a tool call."""

    changed = False
    for label, trace in traces.items():
        offenders = _consult_claims_without_tools(trace)
        if not offenders:
            continue
        changed = True
        prompt_grade = grade["prompts"][label]
        lens = prompt_grade["lenses"]["tool_chain_optimization"]
        lens["score"] = 1
        lens["justification"] = (
            "FAIL: assistant text promised to check/consult/verify but no consult_user tool call "
            "or consult_exchange was recorded."
        )
        flags = prompt_grade.setdefault("red_flags", [])
        if "consult_claim_without_tool_call" not in flags:
            flags.append("consult_claim_without_tool_call")
        prompt_grade["average_score"] = mean(item["score"] for item in prompt_grade["lenses"].values())
    if not changed:
        return
    grade["winner"] = _winner_from_prompt_grades(grade["prompts"]).lower()
    grade["winner_reason"] = "Winner selected after deterministic consult-tool assertions."


def aggregate_scenario(run_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one scenario's run-level grades and trace metrics."""

    prompt_aggregates = {label: _aggregate_prompt(label, run_results) for label in ("A", "B")}
    winner = _winner_from_averages(
        prompt_aggregates["A"]["average_score"],
        prompt_aggregates["B"]["average_score"],
    )
    return {
        "prompts": prompt_aggregates,
        "winner": winner,
        "winner_reason": _winner_reason(winner, prompt_aggregates),
    }


def aggregate_overall(scenario_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate winner across all scenarios."""

    prompt_scores = {}
    for label in ("A", "B"):
        scores = [scenario["aggregate"]["prompts"][label]["average_score"] for scenario in scenario_results]
        prompt_scores[label] = mean(scores) if scores else 0.0
    winner = _winner_from_averages(prompt_scores["A"], prompt_scores["B"])
    return {
        "prompts": prompt_scores,
        "winner": winner,
        "winner_reason": "A %.2f vs B %.2f average across scenarios." % (prompt_scores["A"], prompt_scores["B"]),
    }


def collect_failure_lines(result: dict[str, Any]) -> list[str]:
    """Render deterministic and grader red flags for the report."""

    lines: list[str] = [
        "| Scenario | Run | Prompt | Source | Red Flags |",
        "|---|---:|---:|---|---|",
    ]
    found = False
    for scenario_result in result["scenario_results"]:
        for run in scenario_result["runs"]:
            for label in ("A", "B"):
                trace_flags = run["traces"][label].get("red_flag_summary") or {}
                grader_flags = run["grade"]["prompts"][label].get("red_flags") or []
                combined = []
                combined.extend("%s=%s" % (key, value) for key, value in sorted(trace_flags.items()))
                combined.extend(str(flag) for flag in grader_flags)
                if combined:
                    found = True
                    lines.append(
                        "| %s | %s | %s | %s | %s |"
                        % (
                            scenario_result["scenario"],
                            run["run_index"],
                            label,
                            run["traces"][label].get("output_path", ""),
                            "; ".join(combined),
                        )
                    )
    return lines if found else []


def trace_metrics(trace: dict[str, Any]) -> dict[str, Any]:
    """Summarize token and latency data from one simulator trace."""

    turns = trace.get("turns", [])
    ttfb = [float(turn.get("llm_ttfb_ms") or 0) for turn in turns]
    processing = [float(turn.get("processing_ms") or 0) for turn in turns]
    prompt_tokens = [float(turn.get("prompt_token_count") or 0) for turn in turns]
    usage = _aggregate_usage(turn.get("usage") for turn in turns)
    return {
        "avg_ttfb_ms": mean(ttfb) if ttfb else 0.0,
        "avg_processing_ms": mean(processing) if processing else 0.0,
        "avg_prompt_tokens": mean(prompt_tokens) if prompt_tokens else float(trace.get("prompt_token_count") or 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the phone A/B CLI parser."""

    parser = argparse.ArgumentParser(description="Run A/B prompt simulations for the phone harness.")
    parser.add_argument("--prompt-a", required=True, help='Prompt A module path, or "HEAD" for current phone context')
    parser.add_argument("--prompt-b", required=True, help='Prompt B module path, or "HEAD" for current phone context')
    parser.add_argument("--scenarios", nargs="+", required=True, help="Scenario JSON paths or glob patterns")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--grader", default="four-lens", choices=("four-lens",))
    parser.add_argument("--report", required=True)
    parser.add_argument("--model", default=DEFAULT_PHONE_MODEL)
    parser.add_argument("--grader-model", default=DEFAULT_GRADER_MODEL)
    parser.add_argument("--output-dir", default=str(DEFAULT_AB_OUTPUT_DIR))
    parser.add_argument("--mock-assistant-a", default="", help="No-network assistant text for prompt A")
    parser.add_argument("--mock-assistant-b", default="", help="No-network assistant text for prompt B")
    parser.add_argument("--mock-grader", action="store_true", help="Use deterministic no-network grading")
    parser.add_argument("--no-tools", action="store_true", help="Do not expose phone tool schemas to simulator LLMs")
    return parser


async def main_async(argv: list[str] | None = None) -> int:
    """CLI entrypoint implementation."""

    configure_cli_logging()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    prompt_a = resolve_prompt_variant(args.prompt_a, "A")
    prompt_b = resolve_prompt_variant(args.prompt_b, "B")
    scenarios = [load_scenario(path) for path in expand_scenario_paths(args.scenarios)]
    grader: PairGrader = StaticPairGrader() if args.mock_grader else build_grader_client(model=args.grader_model)
    mock_assistants = {
        label: text
        for label, text in {
            "A": args.mock_assistant_a,
            "B": args.mock_assistant_b or args.mock_assistant_a,
        }.items()
        if text
    }

    result = await run_ab_test(
        scenarios,
        prompt_a=prompt_a,
        prompt_b=prompt_b,
        runs=args.runs,
        grader=grader,
        output_dir=Path(args.output_dir),
        model=args.model,
        include_tools=not args.no_tools,
        mock_assistants=mock_assistants,
    )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown_report(result), encoding="utf-8")
    sys.stdout.write("phone A/B completed\n")
    sys.stdout.write("session: %s\n" % result["ab_session_id"])
    sys.stdout.write("overall_winner: %s\n" % result["overall"]["winner"])
    sys.stdout.write("report: %s\n" % report_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Synchronous CLI entrypoint."""

    return asyncio.run(main_async(argv))


def _normalize_prompt_grade(raw_prompt: dict[str, Any]) -> dict[str, Any]:
    raw_lenses = raw_prompt.get("lenses") if isinstance(raw_prompt.get("lenses"), dict) else {}
    lenses = {}
    for lens in FOUR_LENS_RUBRIC:
        raw_lens = raw_lenses.get(lens["key"]) or raw_lenses.get(lens["label"]) or {}
        if not isinstance(raw_lens, dict):
            raw_lens = {}
        lenses[lens["key"]] = {
            "score": _clamp_score(raw_lens.get("score", 1)),
            "justification": str(raw_lens.get("justification") or "No justification supplied."),
        }
    red_flags = raw_prompt.get("red_flags") if isinstance(raw_prompt.get("red_flags"), list) else []
    return {
        "lenses": lenses,
        "average_score": mean(lens["score"] for lens in lenses.values()),
        "overall_recommendation": str(raw_prompt.get("overall_recommendation") or ""),
        "red_flags": [str(flag) for flag in red_flags],
    }


def _aggregate_prompt(label: str, run_results: list[dict[str, Any]]) -> dict[str, Any]:
    lens_scores = {}
    for lens in FOUR_LENS_RUBRIC:
        scores = [run["grade"]["prompts"][label]["lenses"][lens["key"]]["score"] for run in run_results]
        lens_scores[lens["key"]] = mean(scores) if scores else 0.0
    trace_metrics_list = [run["traces"][label]["metrics"] for run in run_results]
    trace_files = [run["traces"][label]["output_path"] for run in run_results]
    return {
        "lens_scores": lens_scores,
        "average_score": mean(lens_scores.values()) if lens_scores else 0.0,
        "metrics": _aggregate_metrics(trace_metrics_list, trace_files),
    }


def _aggregate_metrics(metrics: list[dict[str, Any]], trace_files: list[str]) -> dict[str, Any]:
    return {
        "avg_ttfb_ms": mean(float(metric.get("avg_ttfb_ms") or 0) for metric in metrics) if metrics else 0.0,
        "avg_processing_ms": (
            mean(float(metric.get("avg_processing_ms") or 0) for metric in metrics) if metrics else 0.0
        ),
        "avg_prompt_tokens": (
            mean(float(metric.get("avg_prompt_tokens") or 0) for metric in metrics) if metrics else 0.0
        ),
        "total_tokens": sum(int(metric.get("total_tokens") or 0) for metric in metrics),
        "trace_files": trace_files,
    }


def _aggregate_usage(usages: Any) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for key in list(totals):
            value = usage.get(key, 0)
            if isinstance(value, int):
                totals[key] += value
    return totals


def _tool_call_names(tool_calls: Any) -> list[str]:
    if not isinstance(tool_calls, list):
        return []
    names: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name")
        if name:
            names.append(str(name))
    return names


def _winner_from_prompt_grades(prompt_grades: dict[str, dict[str, Any]]) -> str:
    return _winner_from_averages(prompt_grades["A"]["average_score"], prompt_grades["B"]["average_score"]).upper()


def _winner_from_averages(score_a: float, score_b: float) -> str:
    if abs(score_a - score_b) < 0.05:
        return "tie"
    return "A" if score_a > score_b else "B"


def _winner_reason(winner: str, prompt_aggregates: dict[str, dict[str, Any]]) -> str:
    return "A %.2f vs B %.2f average four-lens score." % (
        prompt_aggregates["A"]["average_score"],
        prompt_aggregates["B"]["average_score"],
    )


def _clamp_score(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = 1
    return min(5, max(1, score))


def _parse_failure_grade(reason: str, raw_text: str) -> dict[str, Any]:
    grade = {
        "prompt_a": _mock_prompt_grade(1, reason),
        "prompt_b": _mock_prompt_grade(1, reason),
        "winner": "tie",
        "winner_reason": reason,
    }
    grade["prompt_a"]["red_flags"] = ["grader_parse_failure"]
    grade["prompt_b"]["red_flags"] = ["grader_parse_failure"]
    grade["raw_text"] = raw_text[:1000]
    return grade


def _mock_prompt_grade(score: int, justification: str) -> dict[str, Any]:
    return {
        "lenses": {
            lens["key"]: {
                "score": score,
                "justification": justification,
            }
            for lens in FOUR_LENS_RUBRIC
        },
        "overall_recommendation": justification,
        "red_flags": [],
    }


def _mock_score(trace: dict[str, Any]) -> int:
    flag_count = sum(int(value) for value in (trace.get("red_flag_summary") or {}).values())
    return max(1, 5 - flag_count)


def _consult_claims_without_tools(trace: dict[str, Any]) -> list[dict[str, Any]]:
    offenders: list[dict[str, Any]] = []
    for turn in trace.get("turns", []) or []:
        assistant_text = str(turn.get("assistant_text") or "").lower()
        if not any(
            phrase in assistant_text
            for phrase in (
                "i will check",
                "i'll check",
                "i will consult",
                "i'll consult",
                "let me ask",
                "i need to verify",
                "i'll verify",
                "i need to check",
            )
        ):
            continue
        if turn.get("consult_exchanges"):
            continue
        if _tool_call_names(turn.get("tool_calls", [])):
            continue
        offenders.append(turn)
    return offenders


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return (safe or "scenario")[:40]


if __name__ == "__main__":
    raise SystemExit(main())
