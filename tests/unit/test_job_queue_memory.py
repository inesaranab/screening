"""The in-memory JobQueue: the reference behaviour every adapter must match."""

import pytest

from app.adapters.job_queue_memory import InMemoryJobQueue
from app.domain.models import ScreenRequest

_REQ = ScreenRequest(transcript="I am a Quaker.", job_description="Backend")


@pytest.fixture
def queue():
    return InMemoryJobQueue()


@pytest.mark.asyncio
async def test_an_enqueued_job_comes_back_with_its_request(queue):
    """The request travels in the message rather than being stored, so the raw
    transcript is never persisted."""
    await queue.enqueue("abc123", _REQ)

    message = await queue.receive()

    assert message is not None
    assert message.job_id == "abc123"
    assert message.request.transcript == "I am a Quaker."


@pytest.mark.asyncio
async def test_an_empty_queue_returns_none(queue):
    assert await queue.receive() is None


@pytest.mark.asyncio
async def test_a_received_message_is_not_handed_out_again(queue):
    """Two workers polling the same queue must not process one job twice."""
    await queue.enqueue("abc123", _REQ)

    first = await queue.receive()
    second = await queue.receive()

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_messages_come_back_in_order(queue):
    await queue.enqueue("first", _REQ)
    await queue.enqueue("second", _REQ)

    assert (await queue.receive()).job_id == "first"
    assert (await queue.receive()).job_id == "second"


@pytest.mark.asyncio
async def test_deleting_a_message_is_accepted(queue):
    """Deletion is what marks the work done. The in-memory queue removes a
    message on receive, so this is a no-op here and meaningful only in the
    Azure adapter, where an undeleted message reappears for retry."""
    await queue.enqueue("abc123", _REQ)
    message = await queue.receive()

    assert message is not None
    await queue.delete(message)
