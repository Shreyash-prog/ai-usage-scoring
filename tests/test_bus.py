"""EventBus tests (main spec §8.3): priority drop policy + fan-out."""

import asyncio

from app.bus import EventBus, _SessionChannel
from app.models.events import EventType, PersistedEvent


def ev(seq, type) -> PersistedEvent:
    return PersistedEvent(
        id=seq, session_id="s", seq=seq, ts=seq, type=type, payload={}, task_id="001"
    )


async def _fill(ch, types):
    for i, t in enumerate(types, start=1):
        await ch.put(ev(i, t))


async def test_evicts_snapshot_before_paste():
    ch = _SessionChannel(maxsize=3)
    await _fill(ch, [EventType.EDITOR_SNAPSHOT, EventType.EDITOR_PASTE, EventType.CODE_EXECUTED])

    # Full; a high-priority arrival evicts the snapshot first.
    await ch.put(ev(4, EventType.CHAT_RESPONSE_RECEIVED))
    types = [e.type for e in ch._items]
    assert EventType.EDITOR_SNAPSHOT not in types
    assert EventType.EDITOR_PASTE in types
    assert ch.dropped == 1

    # Next high-priority arrival evicts the paste (next in drop order).
    await ch.put(ev(5, EventType.CODE_EXECUTED))
    types = [e.type for e in ch._items]
    assert EventType.EDITOR_PASTE not in types
    assert ch.dropped == 2


async def test_high_priority_never_dropped_soft_overflow():
    ch = _SessionChannel(maxsize=2)
    await _fill(ch, [EventType.CODE_EXECUTED, EventType.TASK_SUBMITTED])
    # Full of high-priority, nothing droppable -> accept overflow rather than lose it.
    await ch.put(ev(3, EventType.SESSION_ENDED))
    assert len(ch._items) == 3
    assert ch.dropped == 0


async def test_incoming_droppable_dropped_when_nothing_to_evict():
    ch = _SessionChannel(maxsize=2)
    await _fill(ch, [EventType.CODE_EXECUTED, EventType.CHAT_PROMPT_SENT])
    # Incoming snapshot is droppable and there is nothing lower to evict -> drop incoming.
    await ch.put(ev(3, EventType.EDITOR_SNAPSHOT))
    assert len(ch._items) == 2
    assert ch.dropped == 1


async def test_fanout_delivers_in_order():
    bus = EventBus(queue_max=100)
    seen: list[int] = []

    async def handler(event: PersistedEvent) -> None:
        seen.append(event.seq)

    bus.subscribe("s", handler)
    await bus.publish(ev(1, EventType.CODE_EXECUTED))
    await bus.publish(ev(2, EventType.CHAT_PROMPT_SENT))
    await asyncio.sleep(0.05)  # let the consumer task drain
    await bus.close_session("s")
    assert seen == [1, 2]
    assert bus.dropped_count("s") == 0
