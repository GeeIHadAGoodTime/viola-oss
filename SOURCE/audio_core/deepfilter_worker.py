"""Persistent JSON/PCM worker for an explicitly configured DeepFilter environment.

This executable imports no Viola modules so its interpreter can have a separate
dependency graph. Model initialization and inference never download resources.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> int:
    protocol = sys.stdout

    def send(message):
        protocol.write(json.dumps(message, separators=(",", ":")) + "\n")
        protocol.flush()

    try:
        model_dir = Path(os.environ.get("VIOLA_DEEPFILTER_MODEL", "")).expanduser().resolve()
        if not (model_dir / "config.ini").is_file() or not (model_dir / "checkpoints").is_dir():
            raise ValueError("Set VIOLA_DEEPFILTER_MODEL to a local model directory with config.ini and checkpoints")
        # Upstream reads DEVICE from its process environment before choosing hardware.
        os.environ["DEVICE"] = "cpu"
        with contextlib.redirect_stdout(sys.stderr):
            import numpy as np
            import torch
            from df.enhance import enhance, init_df
            from df.io import resample

            torch.set_num_threads(1)
            model, state, _ = init_df(
                model_base_dir=str(model_dir),
                post_filter=os.environ.get("VIOLA_DEEPFILTER_POST_FILTER", "1") == "1",
                log_level="ERROR",
                log_file=None,
            )
            model_rate = int(state.sr())
        send({"ready": True, "model_sample_rate": model_rate})
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request.get("op") == "close":
                    return 0
                rate = int(request["sample_rate"])
                if rate <= 0:
                    raise ValueError("Sample rate must be positive")
                values = np.frombuffer(base64.b64decode(request["audio"], validate=True), dtype="<f4").copy()
                if not values.size or not np.isfinite(values).all():
                    raise ValueError("Audio must contain finite samples")
                with contextlib.redirect_stdout(sys.stderr):
                    tensor = torch.from_numpy(values).reshape(1, -1)
                    tensor = resample(tensor, rate, model_rate)
                    enhanced = enhance(model, state, tensor)
                    enhanced = resample(enhanced, model_rate, rate)
                    output = enhanced.detach().cpu().numpy().reshape(-1)
                    output = np.pad(output[: values.size], (0, max(0, values.size - output.size)))
                    if not np.isfinite(output).all():
                        raise ValueError("Model returned nonfinite audio")
                send({"audio": base64.b64encode(output.astype("<f4").tobytes()).decode("ascii")})
            except Exception as exc:
                logger.exception("DeepFilter request failed")
                send({"error": str(exc)})
        return 0
    except Exception as exc:
        logger.exception("DeepFilter initialization failed")
        send({"ready": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
