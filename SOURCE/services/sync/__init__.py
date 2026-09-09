"""Viola Tier-2 cloud-sync engine. See docs/architecture/TIER2_CLOUD_LAUNCH_SPEC.md."""

from services.sync.conflict import (
    IncomingMutation,
    ResolutionOutcome,
    ServerRowSnapshot,
    resolve,
)
from services.sync.consent import (
    SETTINGS_CONSENT_OPTIONAL,
    ConsentRequiredError,
    current_consent_generation,
    has_cloud_sync_consent,
)
from services.sync.cursors import get_cursor, set_cursor
from services.sync.engine import CLOUD_ACTOR_ID, CloudStamp, stamp
from services.sync.hlc import hlc_compare, hlc_merge, hlc_now
from services.sync.journal import journal_since, write_journal
from services.sync.middleware_helpers import set_rls_context
from services.sync.version_vector import (
    VersionVector,
    vv_bump,
    vv_concurrent,
    vv_dominates,
    vv_merge,
)

__all__ = [
    "CLOUD_ACTOR_ID",
    "SETTINGS_CONSENT_OPTIONAL",
    "CloudStamp",
    "ConsentRequiredError",
    "IncomingMutation",
    "ResolutionOutcome",
    "ServerRowSnapshot",
    "VersionVector",
    "current_consent_generation",
    "get_cursor",
    "has_cloud_sync_consent",
    "hlc_compare",
    "hlc_merge",
    "hlc_now",
    "journal_since",
    "resolve",
    "set_cursor",
    "set_rls_context",
    "stamp",
    "vv_bump",
    "vv_concurrent",
    "vv_dominates",
    "vv_merge",
    "write_journal",
]
