from __future__ import annotations

from collections.abc import Mapping


class MusicPlayerTransportController:
    def _compose_backend_summary(
        self,
        *,
        backend_name: str,
        resolver_info: str | None,
        metadata: Mapping[str, object],
        capabilities: Mapping[str, bool],
        capability_reasons: Mapping[str, str],
    ) -> str:
        parts: list[str] = [f"Streaming via {backend_name}"]

        if resolver_info:
            parts.append(f"Resolver: {resolver_info}")

        if metadata:
            parts.append(f"Metadata: {', '.join(sorted(metadata.keys()))}")

        feature_labels = [
            ("Seek", "can_seek"),
            ("Volume", "supports_volume"),
            ("Skip next", "can_skip_next"),
            ("Skip previous", "can_skip_previous"),
        ]
        for label, key in feature_labels:
            enabled = bool(capabilities.get(key, False))
            reason = capability_reasons.get(key)
            if enabled:
                parts.append(f"{label}: on")
            elif reason:
                parts.append(f"{label}: off ({reason})")
            else:
                parts.append(f"{label}: off")

        return "\n".join(parts)
