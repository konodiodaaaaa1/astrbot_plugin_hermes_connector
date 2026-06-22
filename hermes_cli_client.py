"""
Hermes CLI 客户端
- 通过 subprocess 调用 Hermes CLI
- 解析输出获取 session_id 和回复文本
- 支持新会话和继续会话
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from typing import Optional

logger = logging.getLogger("astrbot")

# ── 常量 ──────────────────────────────────────────────
SESSION_ID_RE = re.compile(r"session_id:\s*(\S+)")
RESUME_RE = re.compile(r"↻ Resumed session (\S+)")

# Hermes 会话列表输出的表头/分隔行
LIST_HEADER = "Title                            Preview                                  Last Active   ID"
LIST_SEPARATOR = "─" * 118  # 分隔线

# JSONL 导出字段
EXPORT_SESSION_ID = "id"
EXPORT_MESSAGES = "messages"
EXPORT_MESSAGE_ROLE = "role"
EXPORT_MESSAGE_CONTENT = "content"
EXPORT_STARTED_AT = "started_at"
EXPORT_ENDED_AT = "ended_at"
EXPORT_MODEL = "model"
EXPORT_SOURCE = "source"
EXPORT_TITLE = "title"
EXPORT_MESSAGE_COUNT = "message_count"


class HermesCliError(Exception):
    """Hermes CLI 调用错误"""
    pass


def _find_hermes_binary(custom_path: str | None = None) -> str:
    """查找 Hermes CLI 路径"""
    if custom_path and custom_path != "hermes":
        if os.path.isfile(custom_path):
            return custom_path
        logger.warning(f"配置的 Hermes 路径 '{custom_path}' 不存在，尝试系统 PATH")
    # 尝试 which/where
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
    return "hermes"  # fallback


def _parse_session_id(output: str) -> str | None:
    """从 Hermes 输出中解析 session_id"""
    m = SESSION_ID_RE.search(output)
    if m:
        return m.group(1)
    return None


def _parse_response_text(output: str) -> str:
    """从 Hermes 输出中提取回复文本。

    在 --quiet 模式下，stdout 仅包含纯回复文本（session_id 在 stderr），
    所以直接返回 stdout 全文即可。
    """
    return output.strip()


def _parse_session_id_from_stderr(stderr: str) -> str | None:
    """从 stderr 中解析 session_id。

    Hermes --quiet 模式下，session_id 输出在 stderr，格式为:
        \\nsession_id: 20260622_193701_b2ffc7
    """
    m = SESSION_ID_RE.search(stderr)
    if m:
        return m.group(1)
    return None


def _parse_sessions_list(output: str) -> list[dict]:
    """解析 `hermes sessions list` 的输出"""
    sessions = []
    lines = output.strip().split("\n")
    
    # 找到表头后的数据行
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Title"):
            header_idx = i
            break
    
    if header_idx is None:
        return sessions
    
    data_start = header_idx + 2  # 跳过表头和分隔线
    for line in lines[data_start:]:
        line = line.strip()
        if not line or line.startswith(LIST_SEPARATOR[0]):
            continue
        
        # 解析定宽列：Title(32) Preview(41) Last Active(14) ID
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
    """构建 Hermes 运行环境变量"""
    env = os.environ.copy()
    if workdir:
        env["HERMES_CWD"] = workdir
    return env


async def _run_hermes(args: list[str], timeout: int = 120,
                       workdir: str | None = None,
                       binary: str | None = None) -> tuple[int, str, str]:
    """运行 Hermes CLI 命令，返回 (returncode, stdout, stderr)"""
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
            raise HermesCliError(
                f"Hermes 命令超时（{timeout}s）: {' '.join(cmd[:3])}..."
            )
        
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except FileNotFoundError:
        raise HermesCliError(
            f"Hermes CLI 未找到。请确保已安装 Hermes 或在配置中指定正确路径。"
        )
    except Exception as e:
        raise HermesCliError(f"Hermes 调用失败: {e}")


# ── 对外接口 ──────────────────────────────────────────

async def chat(message: str, *, session_id: str | None = None,
               workdir: str | None = None, model: str | None = None,
               timeout: int = 120, quiet: bool = True,
               binary: str | None = None, yolo: bool = False) -> dict:
    """
    向 Hermes 发送消息。
    
    Args:
        message: 要发送的消息
        session_id: 如果提供，则继续该会话；否则创建新会话
        workdir: Hermes 工作目录
        model: 模型名称（仅新会话时有效）
        timeout: 超时秒数
        quiet: 是否使用安静模式
        binary: Hermes CLI 路径
        yolo: 是否启用 yolo 模式（跳过危险命令确认）
        
    Returns:
        {"session_id": str, "response": str, "is_new": bool}
    """
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

    # --quiet 模式: session_id 在 stderr，回复文本在 stdout
    sid = _parse_session_id_from_stderr(stderr)
    response = _parse_response_text(stdout)

    return {
        "session_id": sid or session_id or "unknown",
        "response": response or "(无输出)",
        "is_new": session_id is None,
    }


async def list_sessions(timeout: int = 15,
                        binary: str | None = None) -> list[dict]:
    """列出所有 Hermes 会话"""
    code, stdout, stderr = await _run_hermes(
        ["sessions", "list"], timeout=timeout, binary=binary
    )
    if code != 0:
        logger.warning(f"获取会话列表失败 (code={code}): {stderr[:200]}")
        return []
    return _parse_sessions_list(stdout)


async def get_session_messages(session_id: str, timeout: int = 30,
                                binary: str | None = None) -> list[dict]:
    """
    获取会话的消息历史。
    通过 `hermes sessions export --session-id <id> -` 导出 JSONL 到 stdout 并解析。
    返回消息列表，每条包含 role 和 content。
    """
    try:
        code, stdout, stderr = await _run_hermes(
            ["sessions", "export", "--session-id", session_id, "-"],
            timeout=timeout, binary=binary
        )
        if code != 0:
            logger.warning(f"导出会话失败 (code={code}): {stderr[:200]}")
            return []
        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("id") == session_id:
                    return data.get("messages", [])
            except json.JSONDecodeError:
                continue
        return []
    except Exception as e:
        logger.warning(f"获取会话消息失败: {e}")
        return []


async def check_health(binary: str | None = None) -> dict:
    """
    检查 Hermes CLI 是否可用。
    返回: {"ok": bool, "version": str, "error": str}
    """
    try:
        code, stdout, stderr = await _run_hermes(
            ["--version"], timeout=10, binary=binary
        )
        if code == 0:
            version = stdout.strip().split("\n")[0] if stdout else "unknown"
            return {"ok": True, "version": version, "error": None}
        return {"ok": False, "version": None, "error": stderr[:200]}
    except Exception as e:
        return {"ok": False, "version": None, "error": str(e)}


async def get_session_detail(session_id: str, timeout: int = 15,
                              binary: str | None = None) -> dict | None:
    """
    获取会话详细信息。
    通过 `hermes sessions export --session-id <id> -` 导出 JSONL 到 stdout 并解析。
    """
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


async def switch_session(session_id: str, timeout: int = 15,
                          binary: str | None = None) -> bool:
    """
    验证会话是否存在且可切换。
    通过尝试列出会话来验证。
    """
    sessions = await list_sessions(timeout=timeout, binary=binary)
    return any(s["id"] == session_id for s in sessions)


async def delete_session(session_id: str, *, timeout: int = 15,
                          binary: str | None = None, force: bool = False) -> tuple[bool, str]:
    """
    删除一个 Hermes 会话。
    
    Returns:
        (success, message)
    """
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


async def prune_sessions(*, older_than: int = 90, source: str | None = None,
                          timeout: int = 30, binary: str | None = None,
                          force: bool = False) -> tuple[bool, str]:
    """
    批量清理旧会话。
    
    Args:
        older_than: 删除超过 N 天的会话（默认 90）
        source: 只清理指定来源的会话
        force: 跳过确认
        
    Returns:
        (success, message)
    """
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


async def rename_session_cmd(session_id: str, title: str, *,
                              timeout: int = 15, binary: str | None = None) -> tuple[bool, str]:
    """
    重命名一个 Hermes 会话。
    
    Returns:
        (success, message)
    """
    try:
        args = ["sessions", "rename", session_id, title]
        code, stdout, stderr = await _run_hermes(args, timeout=timeout, binary=binary)
        if code == 0:
            return True, f"已重命名会话 {session_id[:16]}... 为「{title}」"
        error_msg = stderr.strip() or stdout.strip()
        return False, f"重命名失败: {error_msg[:200]}"
    except HermesCliError as e:
        return False, str(e)
