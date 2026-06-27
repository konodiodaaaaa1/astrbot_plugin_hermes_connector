"""Hermes Hub 会话管理路由。"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import get_current_token
from .hermes_parser import parse_export_line, parse_response_text, parse_session_id, parse_sessions_list
from .hermes_runner import run_hermes
from .sse_manager import sse_manager

logger = logging.getLogger("hermes_hub")
router = APIRouter(prefix="/api")


class CreateSessionBody(BaseModel):
    message: str = ""
    workdir: str | None = None
    model: str | None = None
    timeout: int = 120
    yolo: bool = False


class SendMessageBody(BaseModel):
    text: str
    workdir: str | None = None
    model: str | None = None
    timeout: int = 120
    yolo: bool = False


class RenameBody(BaseModel):
    title: str


class PruneBody(BaseModel):
    older_than: int = 90
    source: str | None = None


@router.get("/sessions")
async def list_sessions(token: dict = Depends(get_current_token)):
    code, stdout, stderr = await run_hermes(["sessions", "list"], timeout=30)
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    sessions = parse_sessions_list(stdout)
    sse_manager.publish("sessions_listed", {"count": len(sessions)})
    return {"sessions": sessions}


@router.post("/sessions")
async def create_session(body: CreateSessionBody, token: dict = Depends(get_current_token)):
    args = ["chat", "-q", body.message, "--quiet"]
    if body.model:
        args.extend(["--model", body.model])
    if body.yolo:
        args.append("--yolo")
    code, stdout, stderr = await run_hermes(
        args, workdir=body.workdir, timeout=body.timeout
    )
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    response = parse_response_text(stdout)
    session_id = parse_session_id(stderr) or parse_session_id(stdout) or "unknown"
    sse_manager.publish("session_created", {"session_id": session_id})
    return {"session_id": session_id, "response": response}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, token: dict = Depends(get_current_token)):
    code, stdout, stderr = await run_hermes(
        ["sessions", "export", "--session-id", session_id, "-"],
        timeout=30,
    )
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    detail = parse_export_line(stdout, session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session": detail}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(50, ge=1, le=200),
    token: dict = Depends(get_current_token),
):
    detail = await _fetch_detail(session_id)
    messages = detail.get("messages", [])[-limit:]
    return {"messages": messages}


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: SendMessageBody,
    token: dict = Depends(get_current_token),
):
    args = ["chat", "--resume", session_id, "-q", body.text, "--quiet"]
    if body.model:
        args.extend(["--model", body.model])
    if body.yolo:
        args.append("--yolo")
    code, stdout, stderr = await run_hermes(
        args, workdir=body.workdir, timeout=body.timeout
    )
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    response = parse_response_text(stdout)
    sse_manager.publish("message_sent", {"session_id": session_id})
    return {"response": response}


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str, token: dict = Depends(get_current_token)):
    code, stdout, stderr = await run_hermes(
        ["chat", "-q", "/stop", "--resume", session_id], timeout=30
    )
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    sse_manager.publish("session_stopped", {"session_id": session_id})
    return {"ok": True}


@router.post("/sessions/{session_id}/rename")
async def rename_session(
    session_id: str,
    body: RenameBody,
    token: dict = Depends(get_current_token),
):
    code, stdout, stderr = await run_hermes(
        ["sessions", "rename", session_id, body.title], timeout=30
    )
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    sse_manager.publish("session_renamed", {"session_id": session_id, "title": body.title})
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, token: dict = Depends(get_current_token)):
    code, stdout, stderr = await run_hermes(
        ["sessions", "delete", "--yes", session_id], timeout=30
    )
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    sse_manager.publish("session_deleted", {"session_id": session_id})
    return {"ok": True}


@router.post("/sessions/prune")
async def prune_sessions(body: PruneBody, token: dict = Depends(get_current_token)):
    args = ["sessions", "prune", f"--older-than={body.older_than or 90}"]
    if body.source:
        args.append(f"--source={body.source}")
    args.append("--yes")
    code, stdout, stderr = await run_hermes(args, timeout=60)
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    sse_manager.publish("sessions_pruned", {"older_than": body.older_than})
    return {"ok": True, "detail": stdout.strip() or "pruned"}


async def _fetch_detail(session_id: str) -> dict:
    code, stdout, stderr = await run_hermes(
        ["sessions", "export", "--session-id", session_id, "-"],
        timeout=30,
    )
    if code != 0:
        raise HTTPException(status_code=500, detail=stderr[:500] or stdout[:500])
    detail = parse_export_line(stdout, session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    return detail
