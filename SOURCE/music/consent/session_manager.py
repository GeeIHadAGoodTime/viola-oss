"""
Consent Service Session Management.

This module contains session management logic
extracted from the main ConsentService class to comply with code constraints.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from core.logging_config import get_logger
from music.consent.exceptions import SessionExpired, SessionNotFound
from music.consent.models import ConsentSession, ConsentSessionStatus, ConsentStep

logger = get_logger(__name__)


class ConsentSessionManager:
    """Handles consent session lifecycle management."""

    def __init__(self, service_instance):
        """
        Initialize session manager.

        Args:
            service_instance: The ConsentService instance
        """
        self.service = service_instance

    def start_session(
        self,
        user_id: str | None,
        provider_ids: list[str],
        redirect_uri: str | None = None,
        session_timeout_minutes: int = 15,
    ) -> ConsentSession:
        """
        Start a new consent session for multiple providers.

        Args:
            user_id: User identifier
            provider_ids: List of provider IDs to link
            redirect_uri: Redirect URI for OAuth callbacks
            session_timeout_minutes: Session timeout in minutes

        Returns:
            New consent session

        Raises:
            ValueError: If providers are invalid
        """
        # Generate session ID
        session_id = str(uuid.uuid4())

        # Validate providers exist
        valid_providers = []
        for provider_id in provider_ids:
            if provider_id not in self.service._providers:
                raise ValueError(f"Unknown provider: {provider_id}")
            valid_providers.append(provider_id)

        # Create session
        session = ConsentSession(
            session_id=session_id,
            user_id=user_id or "",
            provider_ids=valid_providers,
            status=ConsentSessionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=session_timeout_minutes),
            redirect_uri=redirect_uri,
            steps=self._create_session_steps(valid_providers),
        )

        # Store session
        with self.service._session_lock:
            self.service._sessions[session_id] = session

        logger.info("Started consent session %s for providers: %s", session_id, valid_providers)
        return session

    def get_session(self, session_id: str) -> ConsentSession:
        """
        Get a consent session by ID.

        Args:
            session_id: Session identifier

        Returns:
            Consent session

        Raises:
            SessionNotFound: If session doesn't exist
            SessionExpired: If session has expired
        """
        with self.service._session_lock:
            session = self.service._sessions.get(session_id)

        if not session:
            raise SessionNotFound(f"Session {session_id} not found")

        # Check expiration
        if datetime.now(UTC) > session.expires_at:
            # Clean up expired session
            with self.service._session_lock:
                self.service._sessions.pop(session_id, None)
            raise SessionExpired(f"Session {session_id} has expired")

        return session

    def complete_step(
        self,
        session_id: str,
        step_data: dict[str, str],
    ) -> ConsentSession:
        """
        Complete the current step in a consent session.

        Args:
            session_id: Session identifier
            step_data: Step completion data

        Returns:
            Updated consent session

        Raises:
            SessionNotFound: If session doesn't exist
            SessionExpired: If session has expired
            ValueError: If step data is invalid
        """
        session = self.get_session(session_id)

        with self.service._session_lock:
            # Validate current step
            if session.current_step_index >= len(session.steps):
                raise ValueError("Session already completed")

            current_step = session.steps[session.current_step_index]

            # Process step completion
            if current_step.step_type == "oauth":
                # Validate OAuth callback data
                if "code" not in step_data:
                    raise ValueError("OAuth code required")
                if "state" not in step_data:
                    raise ValueError("OAuth state required")

                # Store step data
                current_step.completed_data = {k: v for k, v in step_data.items()}
                current_step.completed_at = datetime.now(UTC)

                # Move to next step
                session.current_step_index += 1

                # Check if session is complete
                if session.current_step_index >= len(session.steps):
                    session.status = ConsentSessionStatus.COMPLETED
                    session.completed_at = datetime.now(UTC)

                    # Process completed session
                    self._process_completed_session(session)

            logger.info(
                "Completed step %s for session %s",
                session.current_step_index - 1,
                session_id,
            )
            return session

    def cancel_session(self, session_id: str) -> None:
        """
        Cancel a consent session.

        Args:
            session_id: Session identifier

        Raises:
            SessionNotFound: If session doesn't exist
        """
        with self.service._session_lock:
            session = self.service._sessions.get(session_id)

        if not session:
            raise SessionNotFound(f"Session {session_id} not found")

        # Mark as cancelled
        with self.service._session_lock:
            session.status = ConsentSessionStatus.CANCELLED
            session.cancelled_at = datetime.now(UTC)

        logger.info("Cancelled consent session %s", session_id)

    def _create_session_steps(self, provider_ids: list[str]) -> list[ConsentStep]:
        """Create steps for a consent session."""
        steps: list[ConsentStep] = []

        for provider_id in provider_ids:
            provider = self.service._providers.get(provider_id)
            if provider:
                step = ConsentStep(
                    provider_id=provider_id,
                    display_name=getattr(provider, "display_name", provider_id),
                    authorization_url=provider.get_authorization_url(),
                    step_type="oauth",
                    step_index=len(steps),
                    required_scopes=provider.get_scopes(),
                )
                steps.append(step)

        return steps

    def _process_completed_session(self, session: ConsentSession) -> None:
        """
        Process a completed consent session.

        Args:
            session: Completed session
        """
        try:
            # Extract tokens from completed steps
            for step in session.steps:
                if step.completed_data and "code" in step.completed_data:
                    provider = self.service._providers.get(step.provider_id)
                    if provider:
                        # Exchange code for tokens
                        token_bundle = provider.exchange_code_for_tokens(
                            step.completed_data["code"], session.redirect_uri
                        )

                        if token_bundle:
                            # Store tokens in vault
                            self.service._vault.store_token_bundle(session.user_id, step.provider_id, token_bundle)

                            logger.info(
                                "Stored credentials for provider %s in session %s",
                                step.provider_id,
                                session.session_id,
                            )

        except Exception as e:
            logger.exception("Failed to process completed session %s: %s", session.session_id, e)

    def cleanup_expired_sessions(self) -> int:
        """
        Clean up expired sessions.

        Returns:
            Number of sessions cleaned up
        """
        now = datetime.now(UTC)
        expired_sessions = []

        with self.service._session_lock:
            for session_id, session in self.service._sessions.items():
                if now > session.expires_at:
                    expired_sessions.append(session_id)

            for session_id in expired_sessions:
                del self.service._sessions[session_id]

        if expired_sessions:
            logger.info("Cleaned up %s expired consent sessions", len(expired_sessions))

        return len(expired_sessions)
