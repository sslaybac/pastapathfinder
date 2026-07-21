"""The stderr progress sink (design.md §3.10; FR-41, AC-41.1/41.2).

The clock is injected rather than waited on: AC-41.1's "at least once every 5 seconds"
is a property of the emission rule, and a test that proves it by sleeping proves it
slowly and flakily.
"""

from __future__ import annotations

import io

from pastapathfinder.progress import PROGRESS_INTERVAL_SECONDS, ProgressSink


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_sink(clock: FakeClock, interval: float = PROGRESS_INTERVAL_SECONDS):
    stream = io.StringIO()
    return ProgressSink(stream, interval=interval, clock=clock), stream


def lines(stream: io.StringIO) -> list[str]:
    return stream.getvalue().splitlines()


def test_interval_bound_is_five_seconds():
    """FR-41's bound is the specified one, defined in exactly one place."""
    assert PROGRESS_INTERVAL_SECONDS == 5.0


def test_phase_start_emits_immediately():
    """A phase that has started but processed nothing must not look like a hang."""
    clock = FakeClock()
    sink, stream = make_sink(clock)
    sink.start_phase("analyzing", total=10)
    assert lines(stream) == ["pastapathfinder: analyzing 0/10"]


def test_progress_lines_carry_processed_out_of_total_at_the_interval():
    """AC-41.1: `processed/total` on stderr, updated at least every 5 seconds."""
    clock = FakeClock()
    sink, stream = make_sink(clock)
    sink.start_phase("analyzing", total=100)

    for _ in range(3):  # three quick files: no clock movement, no new lines
        sink.advance()
    assert lines(stream) == ["pastapathfinder: analyzing 0/100"]

    clock.advance(PROGRESS_INTERVAL_SECONDS)
    sink.advance()
    clock.advance(PROGRESS_INTERVAL_SECONDS)
    sink.advance()

    assert lines(stream) == [
        "pastapathfinder: analyzing 0/100",
        "pastapathfinder: analyzing 4/100",
        "pastapathfinder: analyzing 5/100",
    ]


def test_no_gap_longer_than_the_interval_while_processing():
    """The measurable form of AC-41.1: no two consecutive lines more than 5 s apart."""
    clock = FakeClock()
    sink, stream = make_sink(clock)
    emitted: list[float] = []

    sink.start_phase("analyzing", total=60)
    emitted.append(clock.now)
    for _ in range(60):
        clock.advance(1.0)  # one second per file
        before = len(lines(stream))
        sink.advance()
        if len(lines(stream)) > before:
            emitted.append(clock.now)

    assert emitted[-1] == 60.0  # the completing line is always emitted
    gaps = [later - earlier for earlier, later in zip(emitted, emitted[1:], strict=False)]
    assert gaps, "no progress was emitted at all"
    assert max(gaps) <= PROGRESS_INTERVAL_SECONDS


def test_completion_line_is_always_emitted():
    clock = FakeClock()
    sink, stream = make_sink(clock)
    sink.start_phase("analyzing", total=2)
    sink.advance()
    sink.advance()
    assert lines(stream)[-1] == "pastapathfinder: analyzing 2/2"


def test_unknown_total_emits_activity_for_the_phase():
    """AC-41.2: when the total is unknown, name the phase and show elapsed — not silence."""
    clock = FakeClock()
    sink, stream = make_sink(clock)
    sink.start_phase("discovering sources")
    assert lines(stream) == ["pastapathfinder: discovering sources … 0s"]

    clock.advance(7.0)
    sink.advance()
    assert lines(stream)[-1] == "pastapathfinder: discovering sources … 7s"


def test_heartbeat_keeps_an_opaque_phase_alive():
    """AC-41.2 for a single opaque call: the heartbeat emits without any advance()."""
    stream = io.StringIO()
    sink = ProgressSink(stream, interval=0.01)
    with sink.heartbeat("analyzing (engine build)"):
        deadline = 0
        while len(lines(stream)) < 3 and deadline < 500:
            deadline += 1
            _wait()
    produced = lines(stream)
    assert len(produced) >= 3
    assert all(line.startswith("pastapathfinder: analyzing (engine build) … ") for line in produced)
    assert produced[-1].endswith("s")


def _wait() -> None:
    import time

    time.sleep(0.005)


def test_heartbeat_thread_does_not_outlive_the_phase():
    """A background emitter that keeps writing after the phase would corrupt the output."""
    stream = io.StringIO()
    sink = ProgressSink(stream, interval=0.01)
    with sink.heartbeat("analyzing (engine build)"):
        _wait()
    after = len(lines(stream))
    for _ in range(10):
        _wait()
    assert len(lines(stream)) == after


def test_advance_without_a_phase_is_ignored():
    """Progress reporting must never be able to fail a run."""
    clock = FakeClock()
    sink, stream = make_sink(clock)
    sink.advance()
    sink.end_phase()
    sink.advance()
    assert lines(stream) == []


def test_note_is_unconditional():
    clock = FakeClock()
    sink, stream = make_sink(clock)
    sink.start_phase("analyzing", total=5)
    sink.note("running a full analysis after a cache fallback")
    assert lines(stream)[-1] == "pastapathfinder: running a full analysis after a cache fallback"
