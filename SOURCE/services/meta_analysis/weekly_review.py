"""Weekly meta-analysis -- reviews accumulated data to find patterns and insights.

Gathers recent task journal entries, user model data, memory store contents,
and error patterns.  Feeds them into a structured LLM prompt and produces
actionable outputs:

- Failure pattern detection and auto-filed bug reports
- User behavior trend identification
- Proactive suggestions queued for natural surfacing
- User model updates from discovered patterns

The analysis runs silently.  The user only sees results when they are
actionable and contextually appropriate.

Scheduling: uses asyncio to run at a configurable day/time (default Sunday 22:00).
Also supports manual trigger via intent command.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def _safe_user_segment(user_id: str) -> str:
    """Sanitize a user id for use as a directory name (matches auth.gdpr)."""
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in user_id)


def _analysis_dir_for(user_id: str) -> Path:
    """F-017: per-user meta-analysis directory under the configured data dir.

    Replaces the prior ``Path.cwd() / "data" / "meta_analysis"`` global which
    (a) ignored the platform data-dir canon and (b) co-mingled every owner's
    weekly reports into one cwd-local file tree.
    """
    return get_data_dir() / "users" / _safe_user_segment(user_id) / "meta_analysis"


def _bug_reports_dir_for(user_id: str) -> Path:
    return get_data_dir() / "users" / _safe_user_segment(user_id) / "bug_reports"


_DAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Maximum journal entries to include in analysis prompt
_MAX_JOURNAL_ENTRIES = 50

# Maximum memories to include
_MAX_MEMORIES = 30


def _record_personalization_audit(user_id: str, event_type: str, details: dict[str, Any] | None = None) -> bool:
    try:
        from services.profile.personalization_audit import PersonalizationAuditError, require_personalization_event
    except ImportError as exc:
        logger.warning("Personalization audit unavailable for %s: %s", event_type, exc)
        return False
    try:
        require_personalization_event(user_id, event_type, details=details)
    except PersonalizationAuditError as exc:
        logger.warning("Personalization audit failed for %s: %s", event_type, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------


def _redact_review_text(text: Any) -> str:
    """Redact PII before weekly-review text enters LLM prompts or persisted suggestions."""
    from services.memory.store import redact_pii_output

    return redact_pii_output(str(text or ""))


def _redact_analysis_value(value: Any) -> Any:
    """Recursively redact user-derived strings in LLM analysis output."""
    if isinstance(value, str):
        return _redact_review_text(value)
    if isinstance(value, list):
        return [_redact_analysis_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_analysis_value(item) for key, item in value.items()}
    return value


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("Skipping unreadable weekly-review export file %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    payload["_file"] = path.name
    return payload


def _weekly_bug_report_files(user_id: str) -> list[Path]:
    bug_reports_dir = _bug_reports_dir_for(user_id)
    if not bug_reports_dir.exists():
        return []
    return sorted(bug_reports_dir.glob("meta_*.json"))


def export_weekly_review_records(user_id: str) -> dict[str, list[dict[str, Any]]]:
    """Export persisted weekly-review outputs for GDPR portability."""
    records: dict[str, list[dict[str, Any]]] = {"analyses": [], "bug_reports": []}

    analysis_dir = _analysis_dir_for(user_id)
    if analysis_dir.exists():
        for path in sorted(analysis_dir.glob("*_analysis.json")):
            payload = _read_json_file(path)
            if payload is not None:
                records["analyses"].append(payload)

    for path in _weekly_bug_report_files(user_id):
        payload = _read_json_file(path)
        if payload is not None:
            records["bug_reports"].append(payload)

    return records


def count_weekly_review_records(user_id: str) -> int:
    """Count persisted weekly-review outputs for GDPR deletion preview."""
    analysis_dir = _analysis_dir_for(user_id)
    analysis_count = len(list(analysis_dir.glob("*_analysis.json"))) if analysis_dir.exists() else 0
    return analysis_count + len(_weekly_bug_report_files(user_id))


def delete_weekly_review_records(user_id: str) -> int:
    """Delete persisted weekly-review outputs for GDPR erasure."""
    deleted = 0
    analysis_dir = _analysis_dir_for(user_id)
    if analysis_dir.exists():
        for path in list(analysis_dir.glob("*_analysis.json")):
            try:
                path.unlink()
                deleted += 1
            except OSError:
                logger.debug("Could not delete weekly-review analysis file %s", path)
        try:
            analysis_dir.rmdir()
        except OSError:
            pass

    for path in _weekly_bug_report_files(user_id):
        try:
            path.unlink()
            deleted += 1
        except OSError:
            logger.debug("Could not delete weekly-review bug report file %s", path)

    return deleted


def _gather_journal_data() -> str:
    """Collect recent task journal entries as text.

    Task journal has been removed. Returns empty marker so the meta-analysis
    prompt section is skipped gracefully.
    """
    return "No task journal entries found."


def _resolve_analysis_user_id(user_id: str | None = None) -> str | None:
    """Resolve the owner whose weekly review is being analyzed."""
    if user_id:
        return str(user_id)
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except LookupError:
        return None


def _gather_user_model_data(user_id: str | None = None) -> str:
    """Get current user model as text."""
    uid = _resolve_analysis_user_id(user_id)
    if not uid:
        return "User model unavailable."
    try:
        from services.user_model.profile import get_user_model

        model = get_user_model(user_id=uid)
        summary = model.get_profile_summary()
        return _redact_review_text(summary) if summary else "No user model data yet."
    except Exception:
        logger.exception("Failed to gather user model data for meta-analysis")
        return "User model unavailable."


def _gather_memory_data(user_id: str | None = None) -> str:
    """Get recent memories as text."""
    uid = _resolve_analysis_user_id(user_id)
    if not uid:
        return "No stored memories."
    try:
        from services.memory.store import get_memory_store

        store = get_memory_store()
        memories = store.get_recent(uid, limit=_MAX_MEMORIES)
        if not memories:
            return "No stored memories."

        lines = []
        for mem in memories:
            content = _redact_review_text(mem.content)[:100]
            lines.append(
                "- [%s] %s (accessed %dx, last %s)"
                % (
                    mem.category,
                    content,
                    mem.access_count,
                    mem.accessed_at[:10],
                )
            )
        return "\n".join(lines)

    except Exception:
        logger.exception("Failed to gather memory data for meta-analysis")
        return "Memory data unavailable."


def _gather_error_patterns() -> str:
    """Summarize error patterns from recent diagnostics.

    Task journal has been removed. Returns empty marker.
    """
    return "No task history available yet."


# ---------------------------------------------------------------------------
# Analysis execution
# ---------------------------------------------------------------------------


async def run_weekly_analysis(user_id: str | None = None) -> dict[str, Any] | None:
    """Execute the weekly meta-analysis.

    Gathers data, sends to LLM, saves results, and processes action items.

    Returns:
        Analysis results dict, or None on failure.
    """
    uid = _resolve_analysis_user_id(user_id)
    if not uid:
        logger.info("Skipping weekly meta-analysis without an owner user_id")
        return None
    logger.info("Starting weekly meta-analysis for user=%s", uid)

    from core.user_context import user_scope

    with user_scope(uid):
        # 1. Gather data
        journal_data = _gather_journal_data()
        user_model_data = _gather_user_model_data(uid)
        memory_data = _gather_memory_data(uid)
        error_data = _gather_error_patterns()
        if not _record_personalization_audit(
            uid,
            "weekly_review_invoked",
            {
                "user_model_chars": len(user_model_data),
                "memory_chars": len(memory_data),
                "journal_chars": len(journal_data),
            },
        ):
            return None

        # Check if there's enough data to analyze
        if journal_data == "No task journal entries found." and memory_data == "No stored memories.":
            logger.info("Insufficient data for meta-analysis -- skipping")
            return None

        # 2. Build prompt and call LLM
        analysis_prompt = """You are Viola's meta-cognitive module.  Analyze the following data from the past week and produce insights.

TASK JOURNAL (recent tasks):
%s

USER MODEL (current learned preferences):
%s

USER MEMORIES:
%s

ERROR PATTERNS:
%s

Analyze across these categories:

1. FAILURE PATTERNS: Are certain tools or task types failing repeatedly?  What are the root causes?
2. USER BEHAVIOR TRENDS: Recurring requests that could be automated or anticipated.
3. SELF-HEALING OPPORTUNITIES: Configuration changes or retry strategies that could prevent failures.
4. PROACTIVE SUGGESTIONS: Things the user might benefit from but hasn't asked for.  Be conservative -- only suggest things backed by clear evidence in the data.

RULES:
- Suggestions must earn their interruption.  If in doubt, don't suggest it.
- Only suggest automation for patterns demonstrated 3+ times.
- Be specific.  "Play jazz every morning" is actionable.  "Be more helpful" is not.
- Keep each insight to 1-2 sentences.

Return a JSON object:
{
  "failure_patterns": [
    {"pattern": "...", "frequency": N, "suggested_fix": "..."}
  ],
  "behavior_trends": [
    {"trend": "...", "evidence": "...", "automation_opportunity": "..."}
  ],
  "self_healing": [
    {"issue": "...", "fix": "..."}
  ],
  "suggestions": [
    {"text": "...", "trigger_context": "...", "confidence": 0.0-1.0}
  ],
  "profile_updates": {
    "preferences": [{"category": "...", "key": "...", "value": "..."}],
    "facts": [{"key": "...", "value": "..."}]
  },
  "summary": "2-3 sentence overview of findings"
}"""

        prompt = analysis_prompt % (
            journal_data[:3000],
            user_model_data[:1000],
            memory_data[:2000],
            error_data[:1500],
        )

        try:
            from services.llm.factory import get_llm_handler

            handler = get_llm_handler()
            if handler is None:
                logger.warning("No LLM handler available for meta-analysis")
                return None

            response = await handler.complete(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=(
                    "You are a meta-cognitive analysis module. "
                    "Analyze patterns in user interaction data and produce structured insights. "
                    "Respond with JSON only."
                ),
                max_tokens=1500,
            )

            if not response or not isinstance(response, dict):
                logger.warning("Meta-analysis LLM call returned invalid response")
                return None

            content = response.get("content", "") or response.get("text", "")
            if not content:
                return None

            analysis = _parse_analysis_response(content)
            if analysis is None:
                return None

        except Exception:
            logger.exception("Meta-analysis LLM call failed")
            _record_personalization_audit(uid, "weekly_review_failed", {"stage": "llm_call"})
            return None

        if not _record_personalization_audit(
            uid,
            "weekly_review_completed",
            {
                "suggestion_count": len(analysis.get("suggestions", [])),
                "preference_update_count": len(analysis.get("profile_updates", {}).get("preferences", [])),
                "fact_update_count": len(analysis.get("profile_updates", {}).get("facts", [])),
            },
        ):
            return None

        # 3. Save analysis to disk (per-user)
        _save_analysis(analysis, uid)

        # 4. Process action items
        await _process_action_items(analysis, uid)

        logger.info("Weekly meta-analysis completed successfully for user=%s", uid)
        return analysis


def _parse_analysis_response(content: str) -> dict[str, Any] | None:
    """Parse the LLM's analysis JSON response."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end])
            except json.JSONDecodeError:
                logger.debug("Could not parse meta-analysis response as JSON")
                return None
        else:
            return None

    if not isinstance(parsed, dict):
        return None

    # Ensure expected keys exist
    for key in ("failure_patterns", "behavior_trends", "self_healing", "suggestions"):
        if key not in parsed:
            parsed[key] = []
    if "profile_updates" not in parsed:
        parsed["profile_updates"] = {"preferences": [], "facts": []}
    if "summary" not in parsed:
        parsed["summary"] = ""

    parsed["analyzed_at"] = datetime.now(UTC).isoformat()

    redacted = _redact_analysis_value(parsed)
    return redacted if isinstance(redacted, dict) else None


def _save_analysis(analysis: dict[str, Any], user_id: str) -> None:
    """Save analysis results to <data_dir>/users/<uid>/meta_analysis/YYYY-WW_analysis.json (F-017)."""
    try:
        analysis_dir = _analysis_dir_for(user_id)
        analysis_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        week_label = now.strftime("%Y-W%W")
        filename = "%s_analysis.json" % week_label
        path = analysis_dir / filename

        path.write_text(
            json.dumps(analysis, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Meta-analysis saved to %s", path)
    except Exception:
        logger.exception("Failed to save meta-analysis for user=%s", user_id)


async def _process_action_items(analysis: dict[str, Any], user_id: str | None = None) -> None:
    """Process action items from the analysis."""
    uid = _resolve_analysis_user_id(user_id)
    if not uid:
        logger.info("Skipping meta-analysis action processing without an owner user_id")
        return

    # 1. Auto-file bug reports for failure patterns (per-user)
    _file_bug_reports(analysis.get("failure_patterns", []), uid)

    # 2. Queue suggestions for natural surfacing
    _queue_suggestions(analysis.get("suggestions", []), uid)

    # 3. Apply profile updates
    await _apply_profile_updates(analysis.get("profile_updates", {}), uid)


def _file_bug_reports(patterns: list[dict[str, Any]], user_id: str) -> None:
    """Auto-file bug reports for identified failure patterns (per-user, F-017)."""
    if not patterns:
        return

    try:
        bug_reports_dir = _bug_reports_dir_for(user_id)
        bug_reports_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)

        for i, pattern in enumerate(patterns[:5]):
            if not isinstance(pattern, dict):
                continue
            frequency = pattern.get("frequency", 0)
            if frequency < 2:
                continue  # Only file for repeated failures

            report = {
                "source": "meta_analysis",
                "created_at": now.isoformat(),
                "user_id": user_id,
                "pattern": _redact_review_text(pattern.get("pattern", "")),
                "frequency": frequency,
                "suggested_fix": _redact_review_text(pattern.get("suggested_fix", "")),
                "status": "open",
            }

            filename = "meta_%s_%d.json" % (now.strftime("%Y%m%d"), i)
            path = bug_reports_dir / filename

            if not path.exists():
                path.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info("Bug report filed: %s", path)

    except Exception:
        logger.exception("Failed to file bug reports from meta-analysis for user=%s", user_id)


def _queue_suggestions(suggestions: list[dict[str, Any]], user_id: str | None = None) -> None:
    """Queue high-confidence suggestions for natural surfacing."""
    if not suggestions:
        return
    uid = _resolve_analysis_user_id(user_id)
    if not uid:
        logger.info("Skipping weekly-review suggestions without an owner user_id")
        return

    try:
        from services.user_model.profile import get_user_model

        now_iso = datetime.now(UTC).isoformat()
        queued: list[dict[str, Any]] = []

        for sug in suggestions:
            if not isinstance(sug, dict):
                continue
            confidence = sug.get("confidence", 0.0)
            if confidence < 0.7:
                continue  # Only queue high-confidence suggestions

            queued.append(
                {
                    "text": _redact_review_text(sug.get("text", "")),
                    "trigger_context": _redact_review_text(sug.get("trigger_context", "")),
                    "confidence": confidence,
                    "source": "weekly_review",
                    "created_at": now_iso,
                },
            )

        queued_count = len(queued)
        if queued_count:
            if not _record_personalization_audit(
                uid,
                "weekly_review_suggestions_queued",
                {"suggestion_count": queued_count},
            ):
                return
            model = get_user_model(user_id=uid)
            for suggestion in queued:
                model.add_suggestion(suggestion)
            logger.info("Queued %d weekly-review suggestion(s) for user=%s", queued_count, uid)

    except Exception:
        logger.exception("Failed to queue suggestions from meta-analysis")


async def _apply_profile_updates(updates: dict[str, Any], user_id: str | None = None) -> None:
    """Apply discovered profile updates from the analysis."""
    if not updates:
        return
    uid = _resolve_analysis_user_id(user_id)
    if not uid:
        logger.info("Skipping weekly-review profile updates without an owner user_id")
        return

    try:
        from services.user_model.profile import get_user_model

        preference_candidates = [pref for pref in updates.get("preferences", []) if isinstance(pref, dict)]
        fact_candidates = [fact for fact in updates.get("facts", []) if isinstance(fact, dict)]
        if preference_candidates or fact_candidates:
            if not _record_personalization_audit(
                uid,
                "weekly_review_profile_updates_applied",
                {
                    "preference_update_count": len(preference_candidates),
                    "fact_update_count": len(fact_candidates),
                },
            ):
                return

        model = get_user_model(user_id=uid)
        preference_updates = 0
        fact_updates = 0

        for pref in preference_candidates:
            if model.update_preference(
                pref.get("category", ""),
                pref.get("key", ""),
                pref.get("value", ""),
            ):
                preference_updates += 1

        for fact in fact_candidates:
            if model.add_fact(fact.get("key", ""), fact.get("value", "")):
                fact_updates += 1

        if preference_updates or fact_updates:
            logger.debug(
                "Applied %d weekly-review preference updates and %d fact updates for user=%s",
                preference_updates,
                fact_updates,
                uid,
            )

    except Exception:
        logger.exception("Failed to apply profile updates from meta-analysis")


def get_latest_analysis(user_id: str | None = None) -> dict[str, Any] | None:
    """Load the most recent analysis from disk (per-user, F-017)."""
    uid = _resolve_analysis_user_id(user_id)
    if not uid:
        return None
    try:
        analysis_dir = _analysis_dir_for(uid)
        if not analysis_dir.exists():
            return None

        files = sorted(analysis_dir.glob("*_analysis.json"), reverse=True)
        if not files:
            return None

        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load latest meta-analysis for user=%s", uid)
        return None


def get_analysis_summary(user_id: str | None = None) -> str:
    """Return a human-readable summary of the latest analysis."""
    analysis = get_latest_analysis(user_id)
    if not analysis:
        return "No meta-analysis has been run yet."

    lines = []
    analyzed_at = analysis.get("analyzed_at", "unknown")[:10]
    lines.append("Last analysis: %s" % analyzed_at)

    summary = analysis.get("summary", "")
    if summary:
        lines.append(summary)

    fp = analysis.get("failure_patterns", [])
    if fp:
        lines.append("Failure patterns: %d identified" % len(fp))

    bt = analysis.get("behavior_trends", [])
    if bt:
        lines.append("Behavior trends: %d found" % len(bt))

    sugs = analysis.get("suggestions", [])
    if sugs:
        lines.append("Suggestions: %d pending" % len(sugs))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class WeeklyReviewService:
    """Manages the weekly meta-analysis schedule.

    Runs an asyncio background loop that triggers analysis at the
    configured day/time.  Also supports manual triggers.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._lock = threading.Lock()

    async def start(self) -> None:
        """Begin the weekly schedule loop."""
        if self._running:
            return

        from config.settings import settings

        if not getattr(settings, "meta_analysis_enabled", False):
            logger.debug("Meta-analysis disabled in settings")
            return

        self._running = True
        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("Weekly review service started")

    async def stop(self) -> None:
        """Cancel the schedule loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Weekly review service stopped")

    async def trigger_manual(self, user_id: str | None = None) -> dict[str, Any] | None:
        """Run the analysis immediately (manual trigger)."""
        uid = _resolve_analysis_user_id(user_id)
        if not uid:
            logger.info("Manual meta-analysis skipped without an owner user_id")
            return None
        logger.info("Manual meta-analysis triggered for user=%s", uid)
        return await run_weekly_analysis(uid)

    async def _schedule_loop(self) -> None:
        """Background loop that waits for the configured weekly time.

        F-017: at each scheduled tick the loop enumerates owners that
        should be analyzed and dispatches ``run_weekly_analysis`` with an
        explicit ``user_id``. The previous implementation called the
        function with no owner; outside a request context the resolver
        returned ``None`` and the loop silently no-op'd.
        """
        while self._running:
            try:
                wait_seconds = self._seconds_until_next_run()
                logger.info(
                    "Next meta-analysis in %.1f hours",
                    wait_seconds / 3600,
                )
                await asyncio.sleep(wait_seconds)

                if not self._running:
                    break

                owners = self._resolve_owners_for_periodic_run()
                if not owners:
                    logger.info("Weekly meta-analysis tick: no eligible owners; skipping until next tick")
                    continue

                for owner_uid in owners:
                    if not self._running:
                        break
                    try:
                        await run_weekly_analysis(user_id=owner_uid)
                    except Exception:
                        logger.exception(
                            "Weekly meta-analysis failed for user=%s",
                            owner_uid,
                        )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Weekly review schedule loop error")
                # Wait an hour before retrying on error
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    break

    @staticmethod
    def _resolve_owners_for_periodic_run() -> list[str]:
        """Return the user ids the periodic loop should analyze (F-017).

        Single-tenant desktop: the device user id. Cloud (or any
        environment that explicitly configures owners) can override via
        ``settings.meta_analysis_user_ids``. Returns ``[]`` when no
        explicit owner is configured so the loop never silently dispatches
        anonymously.
        """
        try:
            from config.settings import settings

            configured = getattr(settings, "meta_analysis_user_ids", None) or []
            owners = [str(uid).strip() for uid in configured if str(uid).strip()]
            if owners:
                return owners
        except (ImportError, AttributeError, TypeError):
            logger.debug("Could not read meta_analysis_user_ids from settings", exc_info=True)

        # Desktop default: single-tenant device user id. The cloud surface
        # does not set this, so the cloud loop only runs for explicitly
        # configured owners.
        try:
            from config.settings import settings as _settings

            app_surface = str(getattr(_settings, "app_surface", "desktop")).lower()
        except (ImportError, AttributeError, TypeError):
            app_surface = "desktop"

        try:
            if app_surface == "desktop":
                from core.user_context import get_current_or_device_user_id

                return [get_current_or_device_user_id()]
        except ImportError:
            logger.debug("Could not load desktop device user id for weekly review", exc_info=True)

        return []

    def _seconds_until_next_run(self) -> float:
        """Calculate seconds until the next scheduled run."""
        from config.settings import settings

        target_day_name = getattr(settings, "meta_analysis_day", "sunday").lower()
        target_hour = getattr(settings, "meta_analysis_hour", 22)
        target_day = _DAY_MAP.get(target_day_name, 6)

        now = datetime.now()
        current_day = now.weekday()
        current_hour = now.hour

        # Days until target
        days_ahead = target_day - current_day
        if days_ahead < 0:
            days_ahead += 7
        elif days_ahead == 0 and current_hour >= target_hour:
            days_ahead = 7  # Already passed this week

        # Calculate exact target time
        from datetime import timedelta

        target_dt = now.replace(hour=target_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)

        diff = (target_dt - now).total_seconds()
        return max(60.0, diff)  # At least 60 seconds


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_service_lock = threading.Lock()
_service_singleton: WeeklyReviewService | None = None


def get_weekly_review_service() -> WeeklyReviewService:
    """Return the process-wide WeeklyReviewService singleton."""
    global _service_singleton
    if _service_singleton is None:
        with _service_lock:
            if _service_singleton is None:
                _service_singleton = WeeklyReviewService()
    return _service_singleton


def reset_weekly_review_for_tests() -> None:
    """Dispose of the singleton.  For test suites only."""
    global _service_singleton
    with _service_lock:
        _service_singleton = None
