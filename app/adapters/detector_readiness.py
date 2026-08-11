"""Adapter: waiting for the Article 9 detector to start serving.

The detector scales to zero and takes minutes to load its weights. Platform
ingress closes any single request long before that, so readiness is established
by repeating a short request rather than by holding one open.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class HttpClientLike(Protocol):
    """The subset of an async HTTP client this module uses."""

    async def get(self, url: str) -> Any: ...


def http_probe(client: HttpClientLike, url: str) -> Callable[[], Awaitable[bool]]:
    """Build a readiness probe that calls an endpoint over HTTP.

    Args:
        client: Issues the request. Injected so the caller owns its lifetime
            and its per-request timeout.
        url: The endpoint that answers only once the detector is serving.

    Returns:
        A callable returning True when the endpoint answers 200.
    """

    async def probe() -> bool:
        return (await client.get(url)).status_code == 200

    return probe


async def wait_until_ready(
    probe: Callable[[], Awaitable[bool]],
    *,
    deadline_s: float,
    interval_s: float,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Repeat a readiness probe until it succeeds or the deadline passes.

    The deadline covers elapsed time, not time spent sleeping. A probe against
    an endpoint that is not listening consumes its own timeout before failing,
    so counting only the intervals would let the total reach a multiple of the
    deadline.

    Args:
        probe: Returns True once the detector is serving.
        deadline_s: How long to keep trying, in seconds.
        interval_s: Seconds between attempts.
        sleep: Suspends for the given seconds. Injected so tests need no real
            time.
        now: Reads a monotonic clock, one that only moves forward and is
            unaffected by the system clock being adjusted.

    Returns:
        True if the detector became ready, False if the deadline passed first.
    """
    started = now()
    while True:
        try:
            ready = await probe()
        except Exception:  # noqa: BLE001 - any failure means "not ready yet"
            # The probe is supplied by the caller, so the ways it can fail are
            # not knowable here. A detector that has not started refuses the
            # connection, which is the expected state while it loads rather
            # than a failure to report.
            ready = False
        if ready:
            return True
        if now() - started + interval_s > deadline_s:
            return False
        await sleep(interval_s)
