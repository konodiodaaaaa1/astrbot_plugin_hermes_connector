"""在 Hermes Hub 内部执行 Hermes CLI（本地或 Docker exec）。"""
import asyncio
import os
from typing import Optional

HERMES_BINARY = os.environ.get("HERMES_BINARY", "hermes")
HERMES_CONTAINER = os.environ.get("HERMES_CONTAINER", "")


async def run_hermes(
    args: list[str],
    workdir: str | None = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    env = os.environ.copy()
    if workdir:
        env["HERMES_CWD"] = workdir

    if HERMES_CONTAINER:
        cmd = ["docker", "exec"]
        if workdir:
            cmd.extend(["-e", f"HERMES_CWD={workdir}", "-w", workdir])
        cmd.extend([HERMES_CONTAINER, HERMES_BINARY, *args])
    else:
        cmd = [HERMES_BINARY, *args]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir or None,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return -1, "", f"Hermes 命令超时（{timeout}s）"
    except Exception as e:
        return -1, "", f"Hermes 运行失败: {e}"
