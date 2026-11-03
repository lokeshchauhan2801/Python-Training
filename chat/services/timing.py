"""Per-turn timing for chat replies.

``chat.views.send`` creates one ``TurnTimer`` at view entry and passes it to
the chat services. ``timed(timer, stage)`` logs each stage's duration as
``chat.timing stage=<stage> ms=<ms>`` and, when a timer is given, records it.
Just before the turn is saved the service merges ``timer.to_metadata()`` into
the turn metadata under ``"timing"`` (see ``with_timing``), so the reply bubble
can show it and reloaded history keeps it.

Streaming calls ``timer.mark_first_token()`` when the first text piece
arrives; non-streaming turns leave ``ttft_ms`` as ``None``.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class TurnTimer:
    """Collects stage durations (ms) for one chat turn, measured from ``t0``.

    Thread-safe: retrieval and extraction record from worker threads. A stage
    timed more than once (e.g. one ``llm`` call per Agent step) adds up.
    """

    def __init__(self, t0=None):
        self.t0 = time.perf_counter() if t0 is None else t0
        self.ttft_ms = None
        self.stages = {}
        self._lock = threading.Lock()

    def record(self, stage, ms):
        with self._lock:
            self.stages[stage] = self.stages.get(stage, 0) + int(ms)

    def mark_first_token(self):
        """Record time-to-first-token from ``t0`` (first call wins)."""
        with self._lock:
            if self.ttft_ms is not None:
                return
            self.ttft_ms = int((time.perf_counter() - self.t0) * 1000)
        logger.info("chat.timing stage=%s ms=%d", "ttft", self.ttft_ms)

    def total_ms(self):
        return int((time.perf_counter() - self.t0) * 1000)

    def to_metadata(self):
        with self._lock:
            stages = dict(self.stages)
            ttft_ms = self.ttft_ms
        return {"total_ms": self.total_ms(), "ttft_ms": ttft_ms, "stages": stages}


@contextmanager
def timed(timer, stage):
    """Time the block as ``stage``: always logged, recorded when ``timer`` is set."""
    start = time.perf_counter()
    try:
        yield
    finally:
        ms = int((time.perf_counter() - start) * 1000)
        logger.info("chat.timing stage=%s ms=%d", stage, ms)
        if timer is not None:
            timer.record(stage, ms)


def with_timing(metadata, timer):
    """Return ``metadata`` with ``"timing"`` merged in (a copy; never mutated).

    ``None`` timer -> ``metadata`` unchanged. Other keys are kept as they are.
    """
    if timer is None:
        return metadata
    merged = dict(metadata or {})
    merged["timing"] = timer.to_metadata()
    return merged
