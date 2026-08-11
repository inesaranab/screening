"""The Azure Storage Queue adapter: message encoding and the receive contract."""

import json

import pytest

from app.adapters.job_queue_azure import AzureJobQueue, encode_message
from app.domain.models import MAX_TRANSCRIPT_CHARS, ScreenRequest

_REQ = ScreenRequest(transcript="I am a Quaker.", job_description="Backend")

# Azure Storage Queue rejects a message above this size.
_QUEUE_LIMIT_BYTES = 64 * 1024


def test_a_message_carries_the_job_id_and_the_request():
    payload = json.loads(encode_message("abc123", _REQ))

    assert payload["job_id"] == "abc123"
    assert payload["request"]["transcript"] == "I am a Quaker."


def test_a_maximum_transcript_still_fits_a_queue_message():
    """The transcript cap is derived from the detector's context window, which
    is the tighter of the two limits. This asserts the queue limit stays the
    looser one, so raising the model's context cannot silently break enqueueing.
    """
    biggest = ScreenRequest(
        transcript="x" * MAX_TRANSCRIPT_CHARS,
        job_description="y" * 4_000,
    )

    assert len(encode_message("abc123", biggest).encode("utf-8")) < _QUEUE_LIMIT_BYTES


def test_a_non_ascii_transcript_fits_a_queue_message():
    """Escaping a non-ASCII character into its \\uXXXX form costs six bytes
    where the character itself costs three, so an escaped transcript can pass
    the queue's size limit while its character count is still well inside the
    cap. Encoding must stay proportional to the text's own size."""
    request = ScreenRequest(transcript="漢" * 12_000, job_description="Backend")

    assert len(encode_message("abc123", request).encode("utf-8")) < _QUEUE_LIMIT_BYTES


class _FakeQueueClient:
    """Stands in for ``azure.storage.queue.aio.QueueClient``."""

    def __init__(self, messages: list | None = None):
        self.sent: list[str] = []
        self.deleted: list[str] = []
        self.send_kwargs: list[dict] = []
        self._messages = messages or []

    async def send_message(self, content: str, **kwargs) -> None:
        self.sent.append(content)
        self.send_kwargs.append(kwargs)

    def receive_messages(self, **kwargs):
        messages = self._messages

        class _Iter:
            def __aiter__(self):
                self._i = iter(messages)
                return self

            async def __anext__(self):
                try:
                    return next(self._i)
                except StopIteration:
                    raise StopAsyncIteration

        return _Iter()

    async def delete_message(self, message, pop_receipt=None) -> None:
        self.deleted.append(message)


class _Message:
    def __init__(self, content: str, dequeue_count: int = 1):
        self.content = content
        self.id = "msg-1"
        self.pop_receipt = "receipt-1"
        self.dequeue_count = dequeue_count


@pytest.mark.asyncio
async def test_enqueue_sends_the_encoded_message():
    client = _FakeQueueClient()

    await AzureJobQueue(client).enqueue("abc123", _REQ)

    assert json.loads(client.sent[0])["job_id"] == "abc123"


@pytest.mark.asyncio
async def test_receive_decodes_a_message():
    client = _FakeQueueClient([_Message(encode_message("abc123", _REQ))])

    job = await AzureJobQueue(client).receive()

    assert job is not None
    assert job.job_id == "abc123"
    assert job.request.transcript == "I am a Quaker."
    assert job.receipt is not None


@pytest.mark.asyncio
async def test_receive_returns_none_on_an_empty_queue():
    assert await AzureJobQueue(_FakeQueueClient()).receive() is None


@pytest.mark.asyncio
async def test_enqueue_bounds_how_long_the_message_can_live():
    """The message carries the unredacted transcript. Azure's default lifetime
    keeps an undeliverable one readable for days, so the adapter sets its own."""
    from app.adapters.job_queue_azure import MESSAGE_TTL_SECONDS

    client = _FakeQueueClient()

    await AzureJobQueue(client).enqueue("abc123", _REQ)

    assert client.send_kwargs[0]["time_to_live"] == MESSAGE_TTL_SECONDS
    assert MESSAGE_TTL_SECONDS < 24 * 60 * 60


@pytest.mark.asyncio
async def test_receive_reports_how_many_times_the_message_was_delivered():
    """A message redelivered repeatedly is one no worker can finish. The count
    is what lets the caller stop retrying it."""
    client = _FakeQueueClient(
        [_Message(encode_message("abc123", _REQ), dequeue_count=4)]
    )

    job = await AzureJobQueue(client).receive()

    assert job is not None
    assert job.delivery_count == 4
