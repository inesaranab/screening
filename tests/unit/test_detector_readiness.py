"""Waiting for the detector without holding one connection open."""

import pytest

from app.adapters.detector_readiness import wait_until_ready


async def _no_sleep(seconds: float) -> None:
    """Stand in for asyncio.sleep so the tests do not take real time."""
    return


@pytest.mark.asyncio
async def test_ready_on_the_first_probe_does_not_wait():
    probes = 0

    async def probe() -> bool:
        nonlocal probes
        probes += 1
        return True

    assert await wait_until_ready(probe, deadline_s=60, interval_s=5, sleep=_no_sleep)
    assert probes == 1


@pytest.mark.asyncio
async def test_it_keeps_probing_until_the_detector_answers():
    """A cold detector refuses connections for minutes. Each probe is its own
    short request, so no single one approaches the platform's request limit."""
    answers = iter([False, False, True])
    slept: list[float] = []

    async def probe() -> bool:
        return next(answers)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    assert await wait_until_ready(probe, deadline_s=60, interval_s=5, sleep=sleep)
    assert slept == [5, 5]


@pytest.mark.asyncio
async def test_it_gives_up_at_the_deadline():
    """Bounded, so a detector that never starts fails the job rather than
    occupying a worker for as long as the platform allows."""

    async def probe() -> bool:
        return False

    assert not await wait_until_ready(
        probe, deadline_s=10, interval_s=5, sleep=_no_sleep
    )


@pytest.mark.asyncio
async def test_a_probe_that_raises_counts_as_not_ready():
    """A detector that has not started refuses the connection outright. That is
    the expected state while it loads, not a reason to stop waiting."""
    calls = 0

    async def probe() -> bool:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("connection refused")
        return True

    assert await wait_until_ready(probe, deadline_s=60, interval_s=1, sleep=_no_sleep)
    assert calls == 3


@pytest.mark.asyncio
async def test_http_probe_is_ready_only_on_a_200():
    """The detector answers its model-list endpoint once it is serving. Any
    other status means it is reachable but not yet usable."""
    from app.adapters.detector_readiness import http_probe

    class _Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

    class _Client:
        def __init__(self, status: int):
            self.status = status
            self.requested = ""

        async def get(self, url: str):
            self.requested = url
            return _Response(self.status)

    ok = _Client(200)
    assert await http_probe(ok, "http://detector/v1/models")()
    assert ok.requested == "http://detector/v1/models"

    assert not await http_probe(_Client(503), "http://detector/v1/models")()


@pytest.mark.asyncio
async def test_time_spent_probing_counts_against_the_deadline():
    """A probe against an unreachable endpoint consumes its own timeout before
    failing. Counting only the sleeps would let the total run to a multiple of
    the deadline, past the point at which the caller's own limits apply."""
    clock = {"t": 0.0}
    probes = 0

    async def probe() -> bool:
        nonlocal probes
        probes += 1
        clock["t"] += 30.0  # the probe's own timeout elapses
        return False

    async def sleep(seconds: float) -> None:
        clock["t"] += seconds

    ready = await wait_until_ready(
        probe,
        deadline_s=100,
        interval_s=10,
        sleep=sleep,
        now=lambda: clock["t"],
    )

    assert not ready
    assert clock["t"] <= 100 + 30
    assert probes <= 3
