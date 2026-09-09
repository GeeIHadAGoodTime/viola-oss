"""
Shared Embedding Utilities
==========================

Single source of truth for embedding pooling and normalization.

CRITICAL: Any wake-word classifier training path that pools frame
embeddings should use this helper so preprocessing stays consistent.
"""

from __future__ import annotations

import numpy as np


def pool_embedding(embeddings: np.ndarray, normalize: bool = True) -> np.ndarray:
    """
    Pool and optionally L2-normalize embeddings.

    This function should be used anywhere a classifier consumes pooled
    frame embeddings during training or offline analysis.

    Args:
        embeddings: Raw embeddings from OWW preprocessor.
                   Shape: (n_frames, 96) or (1, n_frames, 96) or (batch, n_frames, 96)
        normalize: Whether to L2 normalize. Default True.

    Returns:
        Pooled embedding of shape (96,), dtype float32
    """
    # Handle different input shapes
    if embeddings.ndim == 3:
        # (batch, frames, 96) or (1, frames, 96) -> take first batch
        embeddings = embeddings[0]  # Now (frames, 96)

    if embeddings.ndim == 2:
        # (frames, 96) -> mean across frames
        pooled = embeddings.mean(axis=0)
    else:
        # Already 1D (96,)
        pooled = embeddings

    if normalize:
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm

    return pooled.astype(np.float32)
