"""解析 Hermes CLI 输出，供 Hermes Hub 使用。"""
import json
import re

SESSION_ID_RE = re.compile(r"session_id:\s*(\S+)")
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)

# Hermes 会话列表输出的表头/分隔行
LIST_HEADER = "Title                            Preview                                  Last Active   ID"
LIST_SEPARATOR = "─" * 118


def parse_session_id(output: str) -> str | None:
    text = _strip_ansi(output)
    m = SESSION_ID_RE.search(text)
    return m.group(1) if m else None


def parse_response_text(output: str) -> str:
    return _strip_ansi(output).strip()


def parse_sessions_list(output: str) -> list[dict]:
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
