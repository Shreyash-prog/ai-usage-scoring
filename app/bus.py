"""EventBus — per-session in-memory pub/sub for PersistedEvents (main spec §8).

One bounded channel per session, one fan-out consumer task per session. Persistence
happens before publish (§8.2), so subscribers always see a stable `seq`.

Backpressure drop policy (§8.3) is PRIORITY-based, not FIFO. When a channel is
full we evict the lowest-priority *droppable* event already queued — editor.snapshot
first, then editor.paste — to make room. chat.*, code.executed, task.*, and
session.* are NEVER dropped: if the queue is full of only those, we accept a small
soft-overflow and log, rather than lose a scoring-critical event. Drops are counted
and surfaced as a dashboard metric.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable

from app.models.events import EventType, PersistedEvent

logger = logging.getLogger(__name__)

Handler = Callable[[PersistedEvent], Awaitable[None]]

# Lowest priority first: the order in which we are willing to drop events.
_DROP_ORDER = [EventType.EDITOR_SNAPSHOT, EventType.EDITOR_PASTE]
_DROPPABLE = set(_DROP_ORDER)


class _SessionChannel:
    """A bounded, priority-aware buffer with one producer-side put and consumer get."""

    def __init__(self, maxsize: int) -> None:
        self._items: deque[PersistedEvent] = deque()
        self._maxsize = maxsize
        self._cond = asyncio.Condition()
        self._closed = False
        self.dropped = 0

    async def put(self, event: PersistedEvent) -> None:
        async with self._cond:
            if len(self._items) >= self._maxsize and not self._evict_one_droppable():
                # Queue is full and nothing droppable was already queued.
                if event.type in _DROPPABLE:
                    self.dropped += 1  # the incoming low-priority event is dropped
                    return
                logger.warning(
                    "EventBus channel full of high-priority events; soft-overflow "
                    "to avoid dropping %s (size=%d)",
                    event.type,
                    len(self._items),
                )
            self._items.append(event)
            self._cond.notify()

    def _evict_one_droppable(self) -> bool:
        """Evict the lowest-priority droppable event (snapshot before paste)."""
        for drop_type in _DROP_ORDER:
            for idx, ev in enumerate(self._items):
                if ev.type == drop_type:
                    del self._items[idx]
                    self.dropped += 1
                    return True
        return False

    async def get(self) -> PersistedEvent | None:
        async with self._cond:
            while not self._items and not self._closed:
                await self._cond.wait()
            if self._items:
                return self._items.popleft()
            return None  # closed and fully drained

    async def close(self) -> None:
        async with self._cond:
            self._closed = True
            self._cond.notify_all()


class EventBus:
    def __init__(self, queue_max: int = 1000) -> None:
        self._channels: dict[str, _SessionChannel] = {}
        self._subscribers: dict[str, list[Handler]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._queue_max = queue_max

    def subscribe(self, session_id: str, handler: Handler) -> None:
        self._subscribers.setdefault(session_id, []).append(handler)

    async def publish(self, event: PersistedEvent) -> None:
        sid = event.session_id
        channel = self._channels.get(sid)
        if channel is None:
            channel = _SessionChannel(self._queue_max)
            self._channels[sid] = channel
            self._tasks[sid] = asyncio.create_task(self._consume(sid, channel))
        await channel.put(event)

    async def _consume(self, session_id: str, channel: _SessionChannel) -> None:
        while True:
            event = await channel.get()
            if event is None:
                break
            for handler in self._subscribers.get(session_id, []):
                try:
                    await handler(event)
                except Exception:
                    logger.exception("EventBus subscriber failed on %s", event.type)

    def dropped_count(self, session_id: str) -> int:
        channel = self._channels.get(session_id)
        return channel.dropped if channel else 0

    async def close_session(self, session_id: str) -> None:
        """Drain the channel, stop the consumer, and release per-session state."""
        channel = self._channels.get(session_id)
        if channel is not None:
            await channel.close()
        task = self._tasks.get(session_id)
        if task is not None:
            await task
        self._channels.pop(session_id, None)
        self._tasks.pop(session_id, None)
        self._subscribers.pop(session_id, None)
