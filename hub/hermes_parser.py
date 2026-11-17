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
    """按实际表头读取列，兼容带 Workspace/Src 的新版 CLI 输出。"""
    sessions = []
    lines = _strip_ansi(output).splitlines()
    header_idx = None
    columns = []
    for i, line in enumerate(lines):
        if line.startswith(("Title", "Preview")) and "Last Active" in line:
            columns = list(re.finditer(r"Title|Preview|Workspace|Last Active|Src|\bID\b", line))
            if not columns or columns[-1].group() != "ID":
                continue
            header_idx = i
            break
    if header_idx is None:
        return sessions
    for line in lines[header_idx + 1:]:
        if not line.strip() or line.lstrip().startswith(("─", "…", "...")):
            continue
        values = {
            col.group(): line[col.start():columns[idx + 1].start() if idx + 1 < len(columns) else None].strip()
            for idx, col in enumerate(columns)
        }
        session_id = values.get("ID", "")
        if not session_id or any(c.isspace() for c in session_id):
            continue
        sessions.append({
            "id": session_id,
            "title": values.get("Title") if values.get("Title") not in (None, "", "—") else None,
            "preview": values.get("Preview") if values.get("Preview") not in (None, "", "—") else None,
            "last_active": values.get("Last Active", ""),
        })
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
