"""The stderr progress sink (design.md §3.10 `progress`; FR-41).

A run that lasts up to the FR-29 bound with no output is indistinguishable from a hang,
so this module owns one channel — stderr — and two shapes of line:

* **Countable phases** (`processed/total`) — emitted at least every
  `PROGRESS_INTERVAL_SECONDS` while files are being processed (AC-41.1).
* **Opaque phases** (`{label} … {elapsed}s`) — emitted when the total is unknown or the
  work is a single call whose insides the pipeline cannot see, so silence is never the
  answer (AC-41.2). design.md §3.10 makes the heartbeat form normative for the mypy
  build phase, which is one such call; `heartbeat()` is the facility task 2.1 drives.

stderr is deliberate: stdout carries the run's human-readable report renderings (D9), and
progress must not contaminate output a caller may be parsing or redirecting.

Both the output stream and the clock are injectable, so AC-41.1's "at least every 5
seconds" is testable against a fake clock rather than by waiting.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

#: FR-41's bound: progress updates at least this often while work is under way.
PROGRESS_INTERVAL_SECONDS = 5.0

#: Every line this module writes carries the tool's name, so interleaved output stays
#: attributable when the tool is driven from a script.
LINE_PREFIX = "pastapathfinder: "


class ProgressSink:
    """The run's progress channel. One instance per run; safe to call from any thread.

    A phase is opened with `start_phase()` (or the `phase()` / `heartbeat()` context
    managers), advanced with `advance()`, and closed with `end_phase()`. Calls made with
    no phase open are ignored rather than raising: progress reporting must never be able
    to fail a run.
    """

    __slots__ = (
        "_clock",
        "_interval",
        "_label",
        "_last",
        "_lock",
        "_processed",
        "_started",
        "_stream",
        "_total",
    )

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        interval: float = PROGRESS_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._interval = float(interval)
        self._clock = clock
        self._lock = threading.Lock()
        self._label: str | None = None
        self._total: int | None = None
        self._processed = 0
        self._started = 0.0
        self._last = 0.0

    # -- emission ----------------------------------------------------------

    def _emit(self, text: str) -> None:
        print(f"{LINE_PREFIX}{text}", file=self._stream, flush=True)

    def _line(self) -> str:
        """The current phase's line, in whichever of the two shapes applies."""
        if self._total is None:
            # AC-41.2: the total is unknown, so report activity and elapsed time.
            return f"{self._label} … {int(self._clock() - self._started)}s"
        return f"{self._label} {self._processed}/{self._total}"

    # -- phases ------------------------------------------------------------

    def start_phase(self, label: str, total: int | None = None) -> None:
        """Open a phase and emit its first line immediately.

        The immediate line matters: it is what distinguishes "started, nothing finished
        yet" from a hang during the first `PROGRESS_INTERVAL_SECONDS` of a phase.
        """
        with self._lock:
            self._label = label
            self._total = None if total is None else int(total)
            self._processed = 0
            self._started = self._last = self._clock()
            self._emit(self._line())

    def advance(self, count: int = 1) -> None:
        """Record `count` processed units, emitting a line when one is due (AC-41.1)."""
        with self._lock:
            if self._label is None:
                return
            self._processed += count
            now = self._clock()
            complete = self._total is not None and self._processed >= self._total
            if now - self._last >= self._interval or complete:
                self._last = now
                self._emit(self._line())

    def end_phase(self) -> None:
        """Close the current phase. Emits nothing: the last `advance()` already did."""
        with self._lock:
            self._label = None
            self._total = None

    def note(self, message: str) -> None:
        """Emit one line unconditionally, outside the interval throttle.

        For statements a user must not miss mid-run — the AC-30.2 fallback notice, for
        instance — that belong on the progress channel rather than in a report.
        """
        with self._lock:
            self._emit(message)

    @contextmanager
    def phase(self, label: str, total: int | None = None) -> Iterator[ProgressSink]:
        """Scope a countable phase; the sink is yielded so callers can `advance()`."""
        self.start_phase(label, total)
        try:
            yield self
        finally:
            self.end_phase()

    @contextmanager
    def heartbeat(self, label: str) -> Iterator[None]:
        """Scope an opaque phase, emitting `{label} … {elapsed}s` while it runs.

        design.md §3.10 makes this normative for work handed to an engine in one call
        whose insides the pipeline cannot see: a background thread keeps the channel
        alive because nothing in the foreground will call `advance()` (AC-41.2). The
        thread is a daemon and is always joined on exit, so it can neither outlive the
        run nor interleave with whatever is printed after the phase.
        """
        self.start_phase(label)
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self._interval):
                with self._lock:
                    if self._label is None:
                        return
                    self._last = self._clock()
                    self._emit(self._line())

        thread = threading.Thread(target=beat, name="pastapathfinder-progress", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
            self.end_phase()
