"""Profile-related service modules.

The legacy ``profile_store`` ProfileStore/UserProfile/MailingAddress stack —
along with ``preflight_profile_for_task`` / ``profile_missing_required_fields``
/ ``required_fields_for_task`` / ``persist_profile_preflight_answer`` and the
``ProfilePreflightResult`` value type — was deleted in R5-P0-H (2026-05-30).
The real, canonical structured profile is ``services.user_profile`` (the
``user_profiles`` table in Postgres/SQLite, fields ``address`` / ``city`` /
``state`` / ``zip_code`` / ``preferred_state_of_incorporation``). Per-turn
context injection of the user's identity/address into the model lives in
``intent.context_builder._build_account_owner_context``, which reads
SettingsManager — not from the deleted profile_store. The
``personalization_audit`` module below stays: it is a generic append-only
audit log unrelated to the deleted preflight stack.
"""

from __future__ import annotations

__all__: list[str] = []
