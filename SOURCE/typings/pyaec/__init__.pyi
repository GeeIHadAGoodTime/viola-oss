from __future__ import annotations

import numpy as np
import numpy.typing as npt

class Aec:
    def __init__(
        self,
        frame_size: int,
        filter_length: int,
        sample_rate: int,
        enable_preprocess: bool = ...,
    ) -> None: ...
    def cancel_echo(
        self,
        rec_buffer: npt.NDArray[np.int16],
        echo_buffer: npt.NDArray[np.int16],
    ) -> npt.NDArray[np.int16]: ...
