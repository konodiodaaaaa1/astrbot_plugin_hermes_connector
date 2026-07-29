import asyncio
import unittest

from hub.sse_manager import SSEManager


class SSEManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_idle_subscription_emits_heartbeat(self):
        manager = SSEManager(heartbeat_interval=0.01)
        stream = manager.subscribe()

        try:
            self.assertEqual(await anext(stream), ": heartbeat\n\n")
        finally:
            await stream.aclose()

    async def test_subscription_emits_published_event(self):
        manager = SSEManager(heartbeat_interval=1)
        stream = manager.subscribe()
        next_item = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)

        manager.publish("session_created", {"session_id": "abc"})

        try:
            self.assertEqual(
                await next_item,
                'event: session_created\ndata: {"session_id": "abc"}\n\n',
            )
        finally:
            await stream.aclose()


if __name__ == "__main__":
    unittest.main()
