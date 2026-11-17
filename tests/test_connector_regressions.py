import asyncio
import ast
import importlib.util
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp.helpers import noop
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_members(relative_path, names, namespace=None):
    tree = ast.parse((ROOT / relative_path).read_text(encoding='utf-8'))
    members = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            members.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == 'HermesConnectorPlugin':
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name in names:
                    child.decorator_list = []
                    members.append(child)
    ns = dict(namespace or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=members, type_ignores=[])), relative_path, 'exec'), ns)
    return SimpleNamespace(**ns)


import re
import aiohttp

cli = load_members('hermes_cli_client.py', {'_parse_sessions_list'}, {'re': re})
cli.get_service = lambda: None
main = load_members('main.py', {
    'tool_check_health', 'tool_list_sessions', 'tool_list_models',
    'tool_approve_all', 'tool_deny_all', 'terminate',
}, {'asyncio': asyncio, '_hcc': cli, 'AstrMessageEvent': object,
    'check_health': None, 'discover_models': None,
    'formatters': load_module('connector_formatters', 'formatters.py')})
main.HermesConnectorPlugin = type('HermesConnectorPlugin', (), vars(main))
main.check_health = None
main.discover_models = None
AsyncHermesHubClient = load_members('hermes_hub_client.py', {
    'AsyncTokenManager', 'AsyncHermesHubClient',
}, {'asyncio': asyncio, 'aiohttp': aiohttp, 'time': __import__('time'),
    'json': __import__('json'), 'logger': logging.getLogger(__name__)}).AsyncHermesHubClient
parse_sessions_list = load_module('connector_parser', 'hub/hermes_parser.py').parse_sessions_list
local_models = load_module('connector_local_models', 'model_discovery.py')
hub_models = load_module('connector_hub_models', 'hub/model_discovery.py')


@pytest.mark.parametrize('parser', [parse_sessions_list, cli._parse_sessions_list])
@pytest.mark.parametrize('layout', ['legacy', 'workspace', 'preview_workspace', 'preview'])
def test_session_columns_follow_actual_header(parser, layout):
    sid = 'local-008c11c7cbbf8042aa327b56c85c09be2aa9ec51a7a7be6c73a0b3734adb8703'
    if layout == 'legacy':
        header = f"{'Title':<32} {'Preview':<40} {'Last Active':<13} ID"
        row = f"{'中文测试':<32} {'hello':<40} {'40m ago':<13} {sid}"
    elif layout == 'workspace':
        header = f"{'Title':<28} {'Workspace':<18} {'Last Active':<13} ID"
        row = f"{'中文测试':<28} {'workspace':<18} {'40m ago':<13} {sid}"
    elif layout == 'preview_workspace':
        header = f"{'Preview':<38} {'Workspace':<18} {'Last Active':<13} {'Src':<16} ID"
        row = f"{'hello':<38} {'workspace':<18} {'40m ago':<13} {'cli':<16} {sid}"
    else:
        header = f"{'Preview':<50} {'Last Active':<13} {'Src':<16} ID"
        row = f"{'hello':<50} {'40m ago':<13} {'cli':<16} {sid}"
    output = f'\x1b[32m{header}\x1b[0m\n' + '─' * 110 + f'\n{row}\n  … 更多未显示 (use --limit 10 to see more)\n'
    sessions = parser(output)
    assert len(sessions) == 1
    assert sessions[0]['id'] == sid
    assert sessions[0]['last_active'] == '40m ago'
    assert sessions[0]['title'] == ('中文测试' if layout in ('legacy', 'workspace') else None)
    assert sessions[0]['preview'] == ('hello' if layout != 'workspace' else None)


@pytest.mark.parametrize('method', ['tool_check_health', 'tool_list_sessions', 'tool_list_models', 'tool_approve_all', 'tool_deny_all'])
def test_parameterless_tools_accept_reason(method):
    inspect.signature(getattr(main.HermesConnectorPlugin, method)).bind(None, None, reason='检查连接')


def test_queries_execute_with_reason(monkeypatch):
    async def run():
        plugin = object.__new__(main.HermesConnectorPlugin)
        plugin._refresh_sessions = AsyncMock()
        plugin.state_mgr = SimpleNamespace(get_sessions_cache=lambda: [])
        monkeypatch.setitem(main.tool_check_health.__globals__, 'check_health', AsyncMock(return_value={'ok': True, 'version': 'test'}))
        monkeypatch.setitem(main.tool_list_models.__globals__, 'discover_models', AsyncMock(return_value={'models': [], 'error': 'test'}))
        for method in ['tool_check_health', 'tool_list_sessions', 'tool_list_models']:
            result = [x async for x in getattr(plugin, method)(None, reason='检查连接')]
            assert result
    asyncio.run(run())


@pytest.mark.parametrize('resume', [False, True])
def test_chat_http_timeout_covers_task_timeout(resume):
    async def run():
        client = AsyncHermesHubClient('http://localhost', 'test', timeout=120)
        client.post_json = AsyncMock(return_value={})
        if resume:
            await client.send_message('sid', 'hello', timeout=1800)
        else:
            await client.create_session('hello', timeout=1800)
        assert client.post_json.call_args.kwargs['timeout'].total >= 1810
    asyncio.run(run())


def test_jwt_refresh_retries_once():
    async def run():
        client = AsyncHermesHubClient('http://localhost', 'test')
        unauthorized = Mock(status=401)
        unauthorized.release.return_value = noop()
        success = Mock(status=200)
        client._ensure_session = AsyncMock()
        client._auth_headers = AsyncMock(return_value={'Authorization': 'Bearer test'})
        client._token_mgr.force_refresh = AsyncMock()
        client._session = SimpleNamespace(request=AsyncMock(side_effect=[unauthorized, success]))
        assert await client.get('/api/sessions') is success
        unauthorized.release.assert_called_once()
        client._token_mgr.force_refresh.assert_awaited_once()
    asyncio.run(run())


def test_terminate_closes_hub_client(monkeypatch):
    async def run():
        plugin = object.__new__(main.HermesConnectorPlugin)
        plugin.progress_monitor = SimpleNamespace(stop_all=Mock())
        plugin._bg_tasks = set()
        service = SimpleNamespace(close=AsyncMock())
        monkeypatch.setattr(cli, 'get_service', lambda: service)
        await plugin.terminate()
        service.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('module', [local_models, hub_models])
def test_named_provider_model_config(module, tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('model:\n  provider: custom:custom\n  default: test-model\nproviders:\n  custom:\n    api: https://provider.example/v1\n    key_env: TEST_PROVIDER_KEY\n', encoding='utf-8')
    monkeypatch.setattr(module, '_HERMES_CONFIG_CANDIDATES', [str(path)])
    cfg = module._read_model_config()
    assert cfg['base_url'] == 'https://provider.example/v1'
    assert cfg['provider'] == 'custom:custom'
    assert cfg['key_env'] == 'TEST_PROVIDER_KEY'
    monkeypatch.setenv('TEST_PROVIDER_KEY', 'fixture-secret')
    assert module._resolve_api_key(cfg['provider'], cfg['base_url'], {}, key_env=cfg['key_env']) == 'fixture-secret'
