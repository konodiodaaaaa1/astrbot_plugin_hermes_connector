"""Hermes Hub SSE 事件管理。"""
import asyncio
import json
from typing import AsyncGenerator


class Event:
    def __init__(self, event: str, data: dict):
        self.event = event
        self.data = data


class SSEManager:
    def __init__(self):
        self._queues: set[asyncio.Queue] = set()

    async def subscribe(self) -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.add(queue)
        try:
            while True:
                event = await queue.get()
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
