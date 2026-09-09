"""Conversation state management exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.conversation.canonical_chain import CanonicalFrameChain
    from services.conversation.session_identity import (
        ResumeRequest,
        SessionBranch,
        SessionIdentity,
        create_branch,
        new_session_id,
        resume_session,
    )
    from services.conversation.state_manager import (
        ConversationStateManager,
        get_conversation_manager,
        get_request_conversation_manager,
        set_request_conversation_manager,
        use_request_manager,
    )

__all__ = [
    "CanonicalFrameChain",
    "ConversationStateManager",
    "ResumeRequest",
    "SessionBranch",
    "SessionIdentity",
    "create_branch",
    "get_conversation_manager",
    "get_request_conversation_manager",
    "new_session_id",
    "resume_session",
    "set_request_conversation_manager",
    "use_request_manager",
]


def __getattr__(name: str) -> Any:
    """Resolve conversation exports without importing state-manager at package import."""

    if name == "CanonicalFrameChain":
        from services.conversation.canonical_chain import CanonicalFrameChain as _CanonicalFrameChain

        globals()[name] = _CanonicalFrameChain
        return _CanonicalFrameChain
    if name in {
        "ResumeRequest",
        "SessionBranch",
        "SessionIdentity",
        "create_branch",
        "new_session_id",
        "resume_session",
    }:
        from services.conversation.session_identity import (
            ResumeRequest,
            SessionBranch,
            SessionIdentity,
            create_branch,
            new_session_id,
            resume_session,
        )

        exports = {
            "ResumeRequest": ResumeRequest,
            "SessionBranch": SessionBranch,
            "SessionIdentity": SessionIdentity,
            "create_branch": create_branch,
            "new_session_id": new_session_id,
            "resume_session": resume_session,
        }
        globals().update(exports)
        return exports[name]
    if name in {
        "ConversationStateManager",
        "get_conversation_manager",
        "get_request_conversation_manager",
        "set_request_conversation_manager",
        "use_request_manager",
    }:
        from services.conversation.state_manager import (
            ConversationStateManager,
            get_conversation_manager,
            get_request_conversation_manager,
            set_request_conversation_manager,
            use_request_manager,
        )

        exports = {
            "ConversationStateManager": ConversationStateManager,
            "get_conversation_manager": get_conversation_manager,
            "get_request_conversation_manager": get_request_conversation_manager,
            "set_request_conversation_manager": set_request_conversation_manager,
            "use_request_manager": use_request_manager,
        }
        globals().update(exports)
        return exports[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
