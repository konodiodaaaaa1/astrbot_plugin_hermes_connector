"""
文件操作
本地模式优先直接操作文件系统；Hub 模式通过 Hermes Chat 读取。
"""

import glob
import logging
import os
from typing import Any

from .hermes_cli_client import chat, HermesCliError, is_hub_mode

logger = logging.getLogger("astrbot")


def _resolve_path(path: str) -> str:
    """解析可能为相对路径的目标。"""
    if not path:
        path = "."
    return os.path.expanduser(path)


async def list_files(path: str = ".", session_id: str | None = None,
                     binary: str | None = None) -> list[str]:
    """列出指定路径的文件。本地模式直接读取；Hub 模式回退到 Hermes。"""
    resolved = _resolve_path(path)
    if not is_hub_mode() and os.path.isdir(resolved):
        try:
            entries = sorted(os.listdir(resolved))
            lines = []
            for e in entries:
                full = os.path.join(resolved, e)
                lines.append(f"{e}/" if os.path.isdir(full) else e)
            return lines
        except Exception as e:
            logger.warning(f"本地列出文件失败 ({resolved}): {e}")
            return []

    # Hub 模式或本地路径不存在时，回退到 Hermes Chat
    try:
        result = await chat(
            f"List files in directory: {path}. "
            f"Return ONLY the file names, one per line. "
            f"Do not include any other text.",
            session_id=session_id,
            timeout=30,
            binary=binary,
        )
        lines = [l.strip() for l in result["response"].split("\n") if l.strip()]
        return [l for l in lines if not l.startswith("total ") and "Permission denied" not in l]
    except HermesCliError as e:
        logger.warning(f"Hermes 列出文件失败: {e}")
        return []
    except Exception as e:
        logger.warning(f"列出文件时发生意外错误: {e}")
        return []


async def search_files(keyword: str, path: str = ".",
                       binary: str | None = None) -> list[str]:
    """搜索文件。本地模式用 os.walk；Hub 模式回退到 Hermes。"""
    resolved = _resolve_path(path)
    if not is_hub_mode() and os.path.isdir(resolved):
        try:
            matches = []
            for root, dirs, files in os.walk(resolved):
                # 简单按文件名/目录名匹配
                for d in dirs:
                    if keyword.lower() in d.lower():
                        matches.append(os.path.join(root, d) + "/")
                for f in files:
                    if keyword.lower() in f.lower():
                        matches.append(os.path.join(root, f))
            return sorted(matches)
        except Exception as e:
            logger.warning(f"本地搜索文件失败 ({resolved}): {e}")
            return []

    try:
        result = await chat(
            f"Search for files matching '{keyword}' under path {path}. "
            f"Return ONLY matching file paths, one per line.",
            timeout=30,
            binary=binary,
        )
        lines = [l.strip() for l in result["response"].split("\n") if l.strip()]
        return lines
    except HermesCliError as e:
        logger.warning(f"Hermes 搜索文件失败: {e}")
        return []
    except Exception as e:
        logger.warning(f"搜索文件时发生意外错误: {e}")
        return []


async def read_file_content(path: str, session_id: str | None = None,
                             binary: str | None = None) -> str | None:
    """读取文件内容。本地模式直接读取；Hub 模式回退到 Hermes。"""
    resolved = _resolve_path(path)
    if not is_hub_mode() and os.path.isfile(resolved):
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"本地读取文件失败 ({resolved}): {e}")
            return None

    try:
        result = await chat(
            f"Read the content of file: {path}",
            session_id=session_id,
            timeout=30,
            binary=binary,
        )
        return result["response"]
    except HermesCliError as e:
        logger.warning(f"Hermes 读取文件失败: {e}")
        return None
    except Exception as e:
        logger.warning(f"读取文件时发生意外错误: {e}")
        return None
