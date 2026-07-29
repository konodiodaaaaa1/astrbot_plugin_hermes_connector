import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest


MAIN_PATH = Path(__file__).parents[1] / "main.py"
PROGRESS_MONITOR_PATH = Path(__file__).parents[1] / "progress_monitor.py"


def _load_main_members(*names, namespace=None):
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    selected = []
    wanted = set(names)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in wanted:
                selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "HermesConnectorPlugin":
            selected.extend(
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in wanted
            )
    loaded = dict(namespace or {})
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(MAIN_PATH), "exec"), loaded)
    return loaded


class ListenerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_listener_does_not_subscribe_without_active_sessions(self):
        calls = []

        async def subscribe_events(**kwargs):
            calls.append(kwargs)
            raise asyncio.CancelledError
            yield

        class FakeAsyncio:
            CancelledError = asyncio.CancelledError
            TimeoutError = asyncio.TimeoutError

            @staticmethod
            async def sleep(_seconds):
                raise asyncio.CancelledError

        namespace = _load_main_members(
            "_hub_event_listener",
            namespace={
                "asyncio": FakeAsyncio,
                "aiohttp": SimpleNamespace(ServerTimeoutError=TimeoutError),
                "logger": SimpleNamespace(
                    debug=lambda *args: None, warning=lambda *args: None
                ),
                "subscribe_events": subscribe_events,
            },
        )
        plugin = SimpleNamespace(
            progress_monitor=SimpleNamespace(has_active_sessions=lambda: False),
            _dispatch_hub_event=lambda *args: None,
        )

        with self.assertRaises(asyncio.CancelledError):
            await namespace["_hub_event_listener"](plugin)

        self.assertEqual(calls, [])

    async def test_terminate_cancels_background_tasks(self):
        namespace = _load_main_members("terminate", namespace={"asyncio": asyncio})
        task = asyncio.create_task(asyncio.sleep(3600))
        plugin = SimpleNamespace(
            progress_monitor=SimpleNamespace(stop_all=lambda: None),
            _bg_tasks={task},
        )

        try:
            await namespace["terminate"](plugin)
            self.assertTrue(task.done())
            self.assertTrue(task.cancelled())
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class StaleListenerTest(unittest.TestCase):
    def test_stale_hub_listeners_are_cancelled(self):
        class FakeTask:
            def __init__(self, qualname):
                self._coro = SimpleNamespace(__qualname__=qualname)
                self.cancelled = False

            def get_coro(self):
                return self._coro

            def cancel(self):
                self.cancelled = True

        stale = FakeTask("HermesConnectorPlugin._hub_event_listener")
        unrelated = FakeTask("OtherPlugin._hub_event_listener")
        fake_asyncio = SimpleNamespace(
            Task=object,
            all_tasks=lambda: {stale, unrelated},
        )
        namespace = _load_main_members(
            "_is_hub_event_listener_task",
            "_cancel_stale_hub_event_listeners",
            namespace={"asyncio": fake_asyncio},
        )

        namespace["_cancel_stale_hub_event_listeners"]()

        self.assertTrue(stale.cancelled)
        self.assertFalse(unrelated.cancelled)


class ActiveSessionTest(unittest.TestCase):
    def test_only_running_monitor_tasks_are_active(self):
        tree = ast.parse(PROGRESS_MONITOR_PATH.read_text(encoding="utf-8"))
        method = next(
            child
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ProgressMonitor"
            for child in node.body
            if isinstance(child, ast.FunctionDef)
            and child.name == "has_active_sessions"
        )
        namespace = {}
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(PROGRESS_MONITOR_PATH), "exec"), namespace)
        has_active_sessions = namespace["has_active_sessions"]

        running_task = SimpleNamespace(done=lambda: False)
        finished_task = SimpleNamespace(done=lambda: True)
        monitor = SimpleNamespace(_bars={})
        self.assertFalse(has_active_sessions(monitor))

        monitor._bars["running"] = SimpleNamespace(finished=False, task=running_task)
        self.assertTrue(has_active_sessions(monitor))

        monitor._bars["running"].task = finished_task
        self.assertFalse(has_active_sessions(monitor))


if __name__ == "__main__":
    unittest.main()
