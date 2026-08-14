"""解析 Hermes CLI 输出，供 Hermes Hub 使用。"""
import json
import re

SESSION_ID_RE = re.compile(r"session_id:\s*(\S+)")
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)

# Hermes 会话列表输出的表头/分隔行
# 注意：Hermes v0.20.0 起列头为 "Title / Workspace / Last Active / ID"，
# 旧版本为 "Title / Preview / Last Active / ID"，解析时动态识别列位置。
LIST_HEADER = "Title                            Preview                                  Last Active   ID"
LIST_SEPARATOR = "─" * 118
SESSION_ID_FORMAT_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")
LAST_ACTIVE_RE = re.compile(r"(just now|today|yesterday|\d+\s*[smhdw]\s*ago|\d+[smhdw])\s*$")


def parse_session_id(output: str) -> str | None:
    text = _strip_ansi(output)
    m = SESSION_ID_RE.search(text)
    return m.group(1) if m else None


def parse_response_text(output: str) -> str:
    return _strip_ansi(output).strip()


def parse_sessions_list(output: str) -> list[dict]:
    """解析 `hermes sessions list` 的输出。

    兼容两种列头布局：
    - v0.20.0+:  Title / Workspace / Last Active / ID
    - 旧版:       Title / Preview / Last Active / ID
    """
    sessions = []
    lines = _strip_ansi(output).strip().split("\n")
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
            # ID 是行末 token（固定格式，不包含空格）
            tokens = line.split()
            if not tokens or not SESSION_ID_FORMAT_RE.match(tokens[-1]):
                continue
            session_id = tokens[-1]
            body = line[: -len(session_id)].rstrip()

            # Last Active 从右端按已知格式匹配（"just now"/"3m ago"/"yesterday"...）
            m = LAST_ACTIVE_RE.search(body)
            last_active = m.group(1) if m else ""
            if m:
                before_la = body[: m.start()].rstrip()
            else:
                before_la = body

            # 第二列（Workspace/Preview）：last_active 前的最后一个 token
            la_tokens = before_la.split()
            if la_tokens:
                second_col = la_tokens[-1]
                title = before_la[: -len(second_col)].strip()
            else:
                second_col = ""
                title = before_la

            sessions.append({
                "id": session_id,
                "title": title if title and title != "—" else None,
                "preview": second_col if second_col and second_col != "—" else None,
                "last_active": last_active,
            })
        except Exception:
            pass
    return sessions


def parse_export_line(output: str, session_id: str) -> dict | None:
    for line in _strip_ansi(output).split("\n"):
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
