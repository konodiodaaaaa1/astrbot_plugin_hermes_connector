"""Hermes Hub SSE 事件管理。"""

import asyncio
import json
from typing import AsyncGenerator


class Event:
    def __init__(self, event: str, data: dict):
        self.event = event
        self.data = data


class SSEManager:
    def __init__(self, heartbeat_interval: float = 30.0):
        self._queues: set[asyncio.Queue] = set()
        self._heartbeat_interval = heartbeat_interval

    async def subscribe(self) -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=self._heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    break
                yield f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"
        finally:
            self._queues.discard(queue)

    def publish(self, event: str, data: dict) -> None:
        ev = Event(event, data)
        for queue in list(self._queues):
            try:
                queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def shutdown(self):
        for queue in list(self._queues):
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        self._queues.clear()


sse_manager = SSEManager()
