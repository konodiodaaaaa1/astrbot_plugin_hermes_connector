"""Hermes 客户端入口。

根据配置决定走本地 subprocess 还是远程 Hermes Hub。
- local 模式：保持原来的 CLI 调用和输出解析。
- hub 模式：通过 HTTP/HTTPS 调用远程 Hermes Hub 的 REST API，并支持 SSE 事件。
"""
import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Optional

from .hermes_hub_client import AsyncHermesHubClient

logger = logging.getLogger("astrbot")

# ── 常量 ──────────────────────────────────────────────
SESSION_ID_RE = re.compile(r"session_id:\s*(\S+)")
RESUME_RE = re.compile(r"↻ Resumed session (\S+)")

LIST_HEADER = "Title                            Preview                                  Last Active   ID"
LIST_SEPARATOR = "─" * 118


class HermesCliError(Exception):
    """Hermes 调用错误"""
    pass


def _find_hermes_binary(custom_path: str | None = None) -> str:
    """查找 Hermes CLI 路径"""
    if custom_path and custom_path != "hermes":
        if os.path.isfile(custom_path):
            return custom_path
        logger.warning(f"配置的 Hermes 路径 '{custom_path}' 不存在，尝试系统 PATH")
    for cmd in ("hermes", "hermes.exe"):
        try:
            result = subprocess.run(
                ["where" if os.name == "nt" else "which", cmd],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                path = result.stdout.strip().split("\n")[0].strip()
                if path:
                    return path
        except Exception:
            pass
    return "hermes"


def _parse_session_id(output: str) -> str | None:
    m = SESSION_ID_RE.search(output)
    return m.group(1) if m else None


def _parse_response_text(output: str) -> str:
    return output.strip()


def _parse_session_id_from_stderr(stderr: str) -> str | None:
    m = SESSION_ID_RE.search(stderr)
    return m.group(1) if m else None


def _parse_sessions_list(output: str) -> list[dict]:
    """解析 `hermes sessions list` 的输出"""
    sessions = []
    lines = output.strip().split("\n")
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Title"):
            header_idx = i
            break
    if header_idx is None:
        return sessions
    data_start = header_idx + 2
    for line in lines[data_start:]:
        line = line.strip()
        if not line or line.startswith(LIST_SEPARATOR[0]):
            continue
        try:
            title = line[:32].strip()
            preview = line[32:73].strip()
            last_active = line[73:87].strip()
            session_id = line[87:].strip()
            if session_id:
                sessions.append({
                    "id": session_id,
                    "title": title if title != "—" else None,
                    "preview": preview if preview != "—" else None,
                    "last_active": last_active,
                })
        except Exception:
            pass
    return sessions


def _build_env(workdir: str | None = None) -> dict:
    env = os.environ.copy()
    if workdir:
        env["HERMES_CWD"] = workdir
    return env


async def _run_hermes(args: list[str], timeout: int = 120,
                       workdir: str | None = None,
                       binary: str | None = None) -> tuple[int, str, str]:
    """运行本地 Hermes CLI 命令，返回 (returncode, stdout, stderr)"""
    cmd = [binary or "hermes"] + args
    logger.debug(f"运行 Hermes: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_env(workdir),
            cwd=workdir or None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HermesCliError(f"Hermes 命令超时（{timeout}s）")
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except FileNotFoundError:
        raise HermesCliError("Hermes CLI 未找到。请确保已安装 Hermes 或在配置中指定正确路径。")
    except Exception as e:
        raise HermesCliError(f"Hermes 调用失败: {e}")


# ── 服务抽象 ──────────────────────────────────────────

class HermesService:
    """Hermes 调用服务抽象"""

    async def health(self, binary: str | None = None) -> dict:
        raise NotImplementedError

    async def chat(self, message: str, *, session_id: str | None = None,
                   workdir: str | None = None, model: str | None = None,
                   timeout: int = 120, quiet: bool = True,
                   binary: str | None = None, yolo: bool = False) -> dict:
        raise NotImplementedError

    async def list_sessions(self, timeout: int = 15, binary: str | None = None) -> list[dict]:
        raise NotImplementedError

    async def get_session_detail(self, session_id: str, timeout: int = 15,
                                  binary: str | None = None) -> dict | None:
        raise NotImplementedError

    async def get_session_messages(self, session_id: str, timeout: int = 30,
                                    binary: str | None = None) -> list[dict]:
        raise NotImplementedError

    async def delete_session(self, session_id: str, *, timeout: int = 15,
                              binary: str | None = None, force: bool = False) -> tuple[bool, str]:
        raise NotImplementedError

    async def prune_sessions(self, *, older_than: int = 90, source: str | None = None,
                              timeout: int = 30, binary: str | None = None,
                              force: bool = False) -> tuple[bool, str]:
        raise NotImplementedError

    async def rename_session_cmd(self, session_id: str, title: str, *,
                                  timeout: int = 15, binary: str | None = None) -> tuple[bool, str]:
        raise NotImplementedError

    async def switch_session(self, session_id: str, timeout: int = 15,
                              binary: str | None = None) -> bool:
        raise NotImplementedError


class LocalHermesService(HermesService):
    """本地 Hermes CLI 模式（原来逻辑）"""

    async def health(self, binary: str | None = None) -> dict:
        try:
            code, stdout, stderr = await _run_hermes(["--version"], timeout=10, binary=binary)
            if code == 0:
                version = stdout.strip().split("\n")[0] if stdout else "unknown"
                return {"ok": True, "version": version, "error": None}
            return {"ok": False, "version": None, "error": stderr[:200]}
        except Exception as e:
            return {"ok": False, "version": None, "error": str(e)}

    async def chat(self, message: str, *, session_id: str | None = None,
                   workdir: str | None = None, model: str | None = None,
                   timeout: int = 120, quiet: bool = True,
                   binary: str | None = None, yolo: bool = False) -> dict:
        args = ["chat", "-q", message]
        if quiet:
            args.append("--quiet")
        if session_id:
            args.extend(["--resume", session_id])
        if model:
            args.extend(["-m", model])
        if yolo:
            args.append("--yolo")

        code, stdout, stderr = await _run_hermes(args, timeout=timeout, workdir=workdir, binary=binary)
        if code != 0:
            error_msg = stderr.strip() or stdout.strip()
            raise HermesCliError(f"Hermes 返回错误 (code={code}): {error_msg[:300]}")
        sid = _parse_session_id_from_stderr(stderr)
        response = _parse_response_text(stdout)
        return {
            "session_id": sid or session_id or "unknown",
            "response": response or "(无输出)",
            "is_new": session_id is None,
        }

    async def list_sessions(self, timeout: int = 15, binary: str | None = None) -> list[dict]:
        code, stdout, stderr = await _run_hermes(["sessions", "list"], timeout=timeout, binary=binary)
        if code != 0:
            logger.warning(f"获取会话列表失败 (code={code}): {stderr[:200]}")
            return []
        return _parse_sessions_list(stdout)

    async def get_session_detail(self, session_id: str, timeout: int = 15,
                                  binary: str | None = None) -> dict | None:
        try:
            code, stdout, stderr = await _run_hermes(
                ["sessions", "export", "--session-id", session_id, "-"],
                timeout=timeout, binary=binary
            )
            if code != 0:
                logger.warning(f"导出会话详情失败 (code={code}): {stderr[:200]}")
                return None
            for line in stdout.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("id") == session_id:
                        return data
                except json.JSONDecodeError:
                    continue
            return None
        except Exception as e:
            logger.warning(f"获取会话详情失败: {e}")
            return None

    async def get_session_messages(self, session_id: str, timeout: int = 30,
                                    binary: str | None = None) -> list[dict]:
        detail = await self.get_session_detail(session_id, timeout=timeout, binary=binary)
        return detail.get("messages", []) if detail else []

    async def switch_session(self, session_id: str, timeout: int = 15,
                              binary: str | None = None) -> bool:
        sessions = await self.list_sessions(timeout=timeout, binary=binary)
        return any(s["id"] == session_id for s in sessions)

    async def delete_session(self, session_id: str, *, timeout: int = 15,
                              binary: str | None = None, force: bool = False) -> tuple[bool, str]:
        try:
            args = ["sessions", "delete"]
            if force:
                args.append("--yes")
            args.append(session_id)
            code, stdout, stderr = await _run_hermes(args, timeout=timeout, binary=binary)
            if code == 0:
                return True, f"已删除会话 {session_id[:16]}..."
            error_msg = stderr.strip() or stdout.strip()
            return False, f"删除失败: {error_msg[:200]}"
        except HermesCliError as e:
            return False, str(e)

    async def prune_sessions(self, *, older_than: int = 90, source: str | None = None,
                              timeout: int = 30, binary: str | None = None,
                              force: bool = False) -> tuple[bool, str]:
        try:
            args = ["sessions", "prune", f"--older-than={older_than}"]
            if source:
                args.append(f"--source={source}")
            if force:
                args.append("--yes")
            code, stdout, stderr = await _run_hermes(args, timeout=timeout, binary=binary)
            if code == 0:
                return True, stdout.strip() or f"已清理 {older_than} 天前的旧会话"
            error_msg = stderr.strip() or stdout.strip()
            return False, f"清理失败: {error_msg[:200]}"
        except HermesCliError as e:
            return False, str(e)

    async def rename_session_cmd(self, session_id: str, title: str, *,
                                  timeout: int = 15, binary: str | None = None) -> tuple[bool, str]:
        try:
            args = ["sessions", "rename", session_id, title]
            code, stdout, stderr = await _run_hermes(args, timeout=timeout, binary=binary)
            if code == 0:
                return True, f"已重命名会话 {session_id[:16]}... 为「{title}」"
            error_msg = stderr.strip() or stdout.strip()
            return False, f"重命名失败: {error_msg[:200]}"
        except HermesCliError as e:
            return False, str(e)


class HubHermesService(HermesService):
    """远程 Hermes Hub 模式"""

    def __init__(self, endpoint: str, access_token: str, timeout: int = 120, verify_ssl: bool = False):
        self._client = AsyncHermesHubClient(endpoint, access_token, timeout=timeout, verify_ssl=verify_ssl)

    async def health(self, binary: str | None = None) -> dict:
        return await self._client.health()

    async def chat(self, message: str, *, session_id: str | None = None,
                   workdir: str | None = None, model: str | None = None,
                   timeout: int = 120, quiet: bool = True,
                   binary: str | None = None, yolo: bool = False) -> dict:
        if session_id:
            data = await self._client.send_message(
                session_id, message,
                workdir=workdir, model=model, timeout=timeout, yolo=yolo
            )
            return {"session_id": session_id, "response": data.get("response", ""), "is_new": False}
        data = await self._client.create_session(
            message,
            workdir=workdir, model=model, timeout=timeout, yolo=yolo
        )
        return {
            "session_id": data.get("session_id", "unknown"),
            "response": data.get("response", ""),
            "is_new": True,
        }

    async def list_sessions(self, timeout: int = 15, binary: str | None = None) -> list[dict]:
        return await self._client.list_sessions()

    async def get_session_detail(self, session_id: str, timeout: int = 15,
                                  binary: str | None = None) -> dict | None:
        try:
            return await self._client.get_session(session_id)
        except Exception:
            return None

    async def get_session_messages(self, session_id: str, timeout: int = 30,
                                    binary: str | None = None) -> list[dict]:
        try:
            return await self._client.get_messages(session_id)
        except Exception:
            return []

    async def delete_session(self, session_id: str, *, timeout: int = 15,
                              binary: str | None = None, force: bool = False) -> tuple[bool, str]:
        try:
            await self._client.delete_session(session_id)
            return True, f"已删除会话 {session_id[:16]}..."
        except Exception as e:
            return False, f"删除失败: {e}"

    async def prune_sessions(self, *, older_than: int = 90, source: str | None = None,
                              timeout: int = 30, binary: str | None = None,
                              force: bool = False) -> tuple[bool, str]:
        try:
            data = await self._client.prune_sessions(older_than=older_than, source=source)
            return True, data.get("detail", f"已清理 {older_than} 天前的旧会话")
        except Exception as e:
            return False, f"清理失败: {e}"

    async def rename_session_cmd(self, session_id: str, title: str, *,
                                  timeout: int = 15, binary: str | None = None) -> tuple[bool, str]:
        try:
            await self._client.rename_session(session_id, title)
            return True, f"已重命名会话 {session_id[:16]}... 为「{title}」"
        except Exception as e:
            return False, f"重命名失败: {e}"

    async def switch_session(self, session_id: str, timeout: int = 15,
                              binary: str | None = None) -> bool:
        try:
            await self._client.get_session(session_id)
            return True
        except Exception:
            return False

    def get_event_stream(self):
        """返回 SSE 异步生成器 (event, data)。"""
        return self._client.subscribe_events()

    async def close(self):
        await self._client.close()


# 全局服务实例
_service: HermesService = LocalHermesService()


def configure_service(config: dict) -> None:
    """根据 AstrBot 配置初始化服务"""
    global _service
    mode = config.get("remote_mode", "local")
    if mode == "hub":
        endpoint = config.get("hub_endpoint", "").strip()
        token = config.get("access_token", "").strip()
        timeout = int(config.get("hub_timeout", 120))
        _raw_verify = config.get("hub_verify_ssl", False)
        if isinstance(_raw_verify, str):
            verify_ssl = _raw_verify.strip().lower() in ("true", "1", "yes", "on")
        else:
            verify_ssl = bool(_raw_verify)
        if not endpoint or not token:
            logger.warning("remote_mode=hub 但 hub_endpoint/access_token 未配置，回退到本地模式")
            _service = LocalHermesService()
            return
        _service = HubHermesService(endpoint, token, timeout=timeout, verify_ssl=verify_ssl)
        logger.info(f"Hermes 客户端已切换到 Hub 模式: {endpoint} (verify_ssl={verify_ssl})")
    else:
        _service = LocalHermesService()


def get_service() -> HermesService:
    return _service


def is_hub_mode() -> bool:
    return isinstance(_service, HubHermesService)


# ── 对外接口（兼容老调用方）──────────────────────────────

async def chat(message: str, *, session_id: str | None = None,
               workdir: str | None = None, model: str | None = None,
               timeout: int = 120, quiet: bool = True,
               binary: str | None = None, yolo: bool = False) -> dict:
    return await _service.chat(
        message, session_id=session_id, workdir=workdir, model=model,
        timeout=timeout, quiet=quiet, binary=binary, yolo=yolo
    )


async def list_sessions(timeout: int = 15, binary: str | None = None) -> list[dict]:
    return await _service.list_sessions(timeout=timeout, binary=binary)


async def check_health(binary: str | None = None) -> dict:
    return await _service.health(binary=binary)


async def get_session_detail(session_id: str, timeout: int = 15,
                              binary: str | None = None) -> dict | None:
    return await _service.get_session_detail(session_id, timeout=timeout, binary=binary)


async def get_session_messages(session_id: str, timeout: int = 30,
                                binary: str | None = None) -> list[dict]:
    return await _service.get_session_messages(session_id, timeout=timeout, binary=binary)


async def delete_session(session_id: str, *, timeout: int = 15,
                          binary: str | None = None, force: bool = False) -> tuple[bool, str]:
    return await _service.delete_session(session_id, timeout=timeout, binary=binary, force=force)


async def prune_sessions(*, older_than: int = 90, source: str | None = None,
                          timeout: int = 30, binary: str | None = None,
                          force: bool = False) -> tuple[bool, str]:
    return await _service.prune_sessions(
        older_than=older_than, source=source, timeout=timeout, binary=binary, force=force
    )


async def rename_session_cmd(session_id: str, title: str, *,
                              timeout: int = 15, binary: str | None = None) -> tuple[bool, str]:
    return await _service.rename_session_cmd(session_id, title, timeout=timeout, binary=binary)


async def switch_session(session_id: str, timeout: int = 15,
                          binary: str | None = None) -> bool:
    return await _service.switch_session(session_id, timeout=timeout, binary=binary)


async def subscribe_events():
    """仅当 Hub 模式时可用。"""
    if isinstance(_service, HubHermesService):
        async for event, data in _service.get_event_stream():
            yield event, data
    else:
        # 本地模式没有 SSE，返回空迭代
        return
        yield
