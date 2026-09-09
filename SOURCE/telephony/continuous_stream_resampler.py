"""Clear-resistant streaming resampler for the telephony outbound audio path.

ROOT CAUSE this fixes (intermittent voice breakup on cloud phone calls):

Pipecat's ``SOXRStreamAudioResampler`` keeps an internal SoX filter
delay-line so that audio resampled in chunks has no clicks at chunk
boundaries. To avoid carrying *stale* history into an unrelated new
stream, it calls ``soxr_stream.clear()`` whenever more than
``CLEAR_STREAM_AFTER_SECS`` (0.2 s) of wall-clock time passes between two
``resample()`` calls (``soxr_stream_resampler.SOXRStreamAudioResampler.
_maybe_clear_internal_state``).

On the phone outbound leg that 0.2 s timer fires constantly. Viola's reply
is streamed sentence-by-sentence: the LLM emits text incrementally, Kokoro
synthesises one sentence, then there is a pause (often > 0.2 s) before the
next sentence's audio is produced. Each pause trips the auto-clear.

The problem is that ``clear()`` does not merely reset history -- it discards
the SoX polyphase filter's delay buffer, so the *next* few chunks fed in
produce FEWER output samples than they should while the filter re-primes
(measured: ~62 ms permanently lost at the 24 kHz->16 kHz Kokoro stage and
~another ~100 ms at the 16 kHz->8 kHz serializer stage, PER clear, and the
loss scales linearly with the number of inter-sentence pauses). The dropped
samples are the *start of the next word/sentence* -- so the recipient hears
Viola's voice break up / clip right after every pause. It is missing audio,
not latency: the surviving audio still arrives on time.

THE FIX: a phone call is ONE continuous outbound audio stream, so the
auto-clear is never wanted on this path. This subclass keeps the SoX filter
history continuous across inter-sentence gaps by making the auto-clear a
no-op. The only thing ``clear()`` guards against -- a faint click when a
genuinely-unrelated stream begins reusing the same resampler -- does not
apply here (each call gets fresh resamplers), and a 1-sample click at stream
start is vastly preferable to dropping the first ~100 ms of every sentence.

See ``tools/devbench/phone_outbound_resampler_oracle.py`` for the offline
reproduction + the per-clear sample-loss measurement, and the
``phone-outbound-continuous-resampler`` Ratchet gate
(``scripts/check_phone_outbound_resampler.py``).
"""

from __future__ import annotations

import time

from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler


class ContinuousStreamAudioResampler(SOXRStreamAudioResampler):
    """A ``SOXRStreamAudioResampler`` that never auto-clears its filter state.

    Identical to the parent in every respect except that the wall-clock
    idle-timeout clear (which silently drops audio when it fires mid-call)
    is disabled. Use this for any single, long-lived, gappy audio stream --
    notably the telephony outbound leg where Viola speaks in sentence bursts
    separated by pauses that would otherwise trip the parent's 0.2 s clear.
    """

    def _maybe_clear_internal_state(self) -> None:
        # Intentionally a no-op: keep the SoX delay-line continuous across
        # inter-sentence pauses so resumed speech is not truncated. We still
        # update the timestamp so the parent's bookkeeping stays consistent.
        self._last_resample_time = time.time()


def create_continuous_stream_resampler(**kwargs) -> ContinuousStreamAudioResampler:
    """Factory mirroring ``pipecat.audio.utils.create_stream_resampler``.

    Returns a resampler safe to reuse for the lifetime of a single phone
    call's outbound audio stream without losing audio across pauses.
    """
    return ContinuousStreamAudioResampler(**kwargs)
