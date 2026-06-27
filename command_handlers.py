"""
Hermes 指令处理器
处理所有 /hermes 子命令
"""

import asyncio
import logging
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api import logger
from astrbot.api.message_components import Plain

from .hermes_cli_client import (
    chat, list_sessions, check_health, get_session_detail,
    get_session_messages, delete_session, prune_sessions,
    rename_session_cmd, HermesCliError,
)
from .formatters import (
    format_session_list, format_session_status, format_response,
    format_error, truncate, format_help,
)
from . import file_ops

logger = logging.getLogger("astrbot")


class CommandHandlers:
    """所有 /hermes 子命令的处理器"""
    
    def __init__(self, plugin):
        self.plugin = plugin
    
    # ── 工具方法 ────────────────────────────────────
    
    def _get_config(self):
        return self.plugin.config
    
    def _get_state(self):
        return self.plugin.state_mgr
    
    def _binary(self):
        return self._get_config().get("hermes_command", "hermes")
    
    def _timeout(self):
        return self._get_config().get("max_timeout", 120)
    
    def _workdir(self):
        return self._get_config().get("hermes_workdir", "") or None
    
    def _model(self):
        return self._get_config().get("hermes_model", "") or None
    
    def _yolo(self):
        return self._get_config().get("hermes_approval_mode", "normal") == "yolo"
    
    async def _refresh_sessions(self):
        """刷新会话缓存"""
        try:
            sessions = await list_sessions(binary=self._binary())
            self._get_state().update_sessions_cache(sessions)
            return sessions
        except Exception as e:
            logger.warning(f"刷新 Hermes 会话列表失败: {e}")
            return []
    
    async def _resolve_session(self, event, arg: str | None = None) -> dict | None:
        """
        解析会话参数。
        arg 可以是序号、ID 前缀或 None（使用当前会话）。
        返回 {"session": dict, "idx": int} 或 None
        """
        state = self._get_state()
        window_id = event.unified_msg_origin
        
        if not arg:
            # 使用当前会话
            sid = state.get_current_session(window_id)
            if not sid:
                return None
            return {"session": {"id": sid}, "idx": None}
        
        # 尝试序号
        try:
            idx = int(arg)
            session = state.get_session_by_idx(idx)
            if session:
                return {"session": session, "idx": idx}
        except ValueError:
            pass
        
        # 尝试 ID 前缀
        session = state.find_session_by_id_prefix(arg)
        if session:
            return {"session": session, "idx": None}
        
        return None
    
    async def send_reply(self, event: AstrMessageEvent, text: str):
        """发送回复到当前聊天"""
        await event.send(MessageChain(chain=[Plain(text)]))
    
    # ── 命令实现 ────────────────────────────────────
    
    async def cmd_list(self, event: AstrMessageEvent, arg: str | None = None):
        """列出会话"""
        sessions = await self._refresh_sessions()
        if arg == "all":
            text = format_session_list(sessions) + "\n\n" + self._get_state().to_dict()
        else:
            text = format_session_list(sessions)
        await self.send_reply(event, text)
    
    async def cmd_status(self, event: AstrMessageEvent, arg: str | None = None):
        """查看会话状态"""
        resolved = await self._resolve_session(event, arg)
        if not resolved:
            await self.send_reply(event, "⚠️ 未指定会话。请提供序号或会话 ID，或先用 `/hermes sw` 切换到某个会话。")
            return
        
        session_id = resolved["session"]["id"]
        detail = await get_session_detail(session_id, binary=self._binary())
        text = format_session_status(session_id, detail)
        await self.send_reply(event, text)
    
    async def cmd_switch(self, event: AstrMessageEvent, arg: str | None = None):
        """切换当前会话"""
        if not arg:
            await self.send_reply(event, "⚠️ 请提供会话序号或 ID。例如: `/hermes sw 1`")
            return
        
        sessions = await self._refresh_sessions()
        resolved = await self._resolve_session(event, arg)
        
        if not resolved:
            await self.send_reply(event, f"⚠️ 找不到会话: {arg}")
            return
        
        session = resolved["session"]
        window_id = event.unified_msg_origin
        self._get_state().set_current_session(window_id, session["id"], resolved["idx"])
        
        preview = session.get("preview") or "无预览"
        await self.send_reply(
            event,
            f"✅ 已切换到会话: `{session['id'][:16]}...`\n预览: {truncate(preview, 100)}"
        )
    
    async def cmd_to(self, event: AstrMessageEvent, arg: str | None = None):
        """发送消息到指定会话"""
        # 解析 "序号 消息内容"
        if not arg:
            await self.send_reply(event, "⚠️ 用法: `/hermes to <序号> <消息>`")
            return
        
        parts = arg.strip().split(" ", 1)
        if len(parts) < 2:
            await self.send_reply(event, "⚠️ 用法: `/hermes to <序号> <消息>`")
            return
        
        idx_str, message = parts
        
        # 刷新缓存
        await self._refresh_sessions()
        resolved = await self._resolve_session(event, idx_str)
        
        if not resolved:
            await self.send_reply(event, f"⚠️ 找不到会话: {idx_str}")
            return
        
        session_id = resolved["session"]["id"]

        # 非阻塞模式：后台发送 + 进度监控
        if self.plugin.progress_monitor.enabled:
            await self.send_reply(event, f"✅ 已提交到 Hermes 会话 [{session_id[:16]}...]，后台执行中。")
            task = asyncio.create_task(
                self.plugin._background_chat(event, message, session_id, resolved.get("idx"))
            )
            self.plugin._track_bg_task(task)
            return

        # 阻塞模式（fallback）
        try:
            # 发送消息
            await self.send_reply(event, f"⏳ 正在发送到 {session_id[:16]}...")
            
            result = await chat(
                message,
                session_id=session_id,
                workdir=self._workdir(),
                model=self._model(),
                timeout=self._timeout(),
                binary=self._binary(),
                yolo=self._yolo(),
            )
            
            # 更新当前会话
            window_id = event.unified_msg_origin
            self._get_state().set_current_session(window_id, result["session_id"], resolved["idx"])
            
            text = format_response(result["session_id"], truncate(result["response"]), result["is_new"])
            await self.send_reply(event, text)
        except HermesCliError as e:
            await self.send_reply(event, format_error(str(e)))
    
    async def cmd_send(self, event: AstrMessageEvent, message: str = ""):
        """发送消息到当前会话"""
        if not message:
            await self.send_reply(event, "⚠️ 请输入消息内容")
            return
        
        resolved = await self._resolve_session(event, None)
        if not resolved:
            # 如果 auto_create 开启，自动创建新会话
            if self._get_config().get("auto_create_session", True):
                await self.send_reply(event, "⏳ 当前没有活跃会话，正在创建新会话...")
                await self.cmd_create(event, message)
                return
            else:
                await self.send_reply(event, "⚠️ 当前没有活跃会话。请先用 `/hermes create <提示词>` 创建会话。")
                return
        
        session_id = resolved["session"]["id"]

        # 非阻塞模式：后台发送 + 进度监控
        if self.plugin.progress_monitor.enabled:
            await self.send_reply(event, f"✅ 已提交到 Hermes 会话 [{session_id[:16]}...]，后台执行中。")
            task = asyncio.create_task(
                self.plugin._background_chat(event, message, session_id, resolved.get("idx"))
            )
            self.plugin._track_bg_task(task)
            return

        # 阻塞模式（fallback）
        try:
            result = await chat(
                message,
                session_id=session_id,
                workdir=self._workdir(),
                model=self._model(),
                timeout=self._timeout(),
                binary=self._binary(),
                yolo=self._yolo(),
            )
            
            window_id = event.unified_msg_origin
            self._get_state().set_current_session(window_id, result["session_id"])
            
            text = format_response(result["session_id"], truncate(result["response"]))
            await self.send_reply(event, text)
        except HermesCliError as e:
            await self.send_reply(event, format_error(str(e)))
    
    async def cmd_create(self, event: AstrMessageEvent, prompt: str = ""):
        """创建新会话"""
        if not prompt:
            await self.send_reply(
                event,
                "⚠️ 请提供初始提示词。\n用法: `/hermes create <提示词>`\n例如: `/hermes create 帮我写一个 Python 爬虫`"
            )
            return
        
        # 非阻塞模式：后台创建 + 进度监控
        if self.plugin.progress_monitor.enabled:
            await self.send_reply(event, f"⏳ 正在创建 Hermes 新会话...后台执行中，有进展时会通知你。")
            task = asyncio.create_task(
                self.plugin._background_create(event, prompt)
            )
            self.plugin._track_bg_task(task)
            return

        # 阻塞模式（fallback）
        await self.send_reply(event, f"⏳ 正在创建 Hermes 新会话...")
        
        try:
            result = await chat(
                prompt,
                workdir=self._workdir(),
                model=self._model(),
                timeout=self._timeout(),
                binary=self._binary(),
                yolo=self._yolo(),
            )
            
            # 更新状态
            window_id = event.unified_msg_origin
            self._get_state().set_current_session(window_id, result["session_id"])
            
            # 刷新缓存
            await self._refresh_sessions()
            
            text = (
                f"✅ **新会话已创建**\n"
                f"- Session ID: `{result['session_id'][:16]}...`\n\n"
                f"{truncate(result['response'])}"
            )
            await self.send_reply(event, text)
        except HermesCliError as e:
            await self.send_reply(event, format_error(str(e)))
    
    async def cmd_health(self, event: AstrMessageEvent, arg: str | None = None):
        """检查 Hermes 连接状态"""
        try:
            health = await check_health(binary=self._binary())
            if health["ok"]:
                text = (
                    f"✅ **Hermes Agent 连接正常**\n"
                    f"- 版本: `{health['version']}`\n"
                    f"- 路径: `{self._binary()}`\n"
                    f"- 默认模型: `{self._model() or 'Hermes 默认'}`\n"
                    f"- 工作目录: `{self._workdir() or 'Hermes 默认'}`"
                )
            else:
                text = (
                    f"❌ **Hermes Agent 连接异常**\n"
                    f"- 错误: {health['error']}\n"
                    f"- 路径: `{self._binary()}`\n"
                    f"\n💡 请检查:\n"
                    f"1. Hermes 是否已安装 (`hermes --version`)\n"
                    f"2. PATH 环境变量是否包含 Hermes\n"
                    f"3. 插件配置中的 hermes_command 路径是否正确"
                )
            self._get_state().set_hermes_health(health)
            await self.send_reply(event, text)
        except Exception as e:
            await self.send_reply(event, format_error(str(e)))
    
    async def cmd_messages(self, event: AstrMessageEvent, arg: str | None = None):
        """查看最近消息"""
        resolved = await self._resolve_session(event, None if not arg else arg)
        if not resolved:
            await self.send_reply(event, "⚠️ 未指定会话。")
            return
        
        session_id = resolved["session"]["id"]
        
        try:
            messages = await get_session_messages(
                session_id, timeout=30, binary=self._binary()
            )
            
            if not messages:
                await self.send_reply(event, f"📭 会话 `{session_id[:16]}...` 暂无消息。")
                return
            
            # 格式化最近 10 条消息
            recent = messages[-10:]
            lines = [f"📜 **最近消息** (`{session_id[:16]}...`)\n"]
            
            for msg in recent:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
                
                role_icon = {"user": "👤", "assistant": "🤖", "tool": "🔧"}.get(role, "❓")
                content_preview = truncate(str(content), 200)
                lines.append(f"{role_icon} **{role}**: {content_preview}")
            
            await self.send_reply(event, "\n".join(lines))
        except Exception as e:
            await self.send_reply(event, format_error(str(e)))

    async def cmd_rename(self, event: AstrMessageEvent, arg: str | None = None):
        """重命名当前会话"""
        if not arg:
            await self.send_reply(event, "⚠️ 请提供新名称。用法: `/hermes rename <名称>`")
            return

        resolved = await self._resolve_session(event, None)
        if not resolved:
            await self.send_reply(event, "⚠️ 当前没有活跃会话。")
            return

        session_id = resolved["session"]["id"]
        ok, msg = await rename_session_cmd(
            session_id, arg, binary=self._binary()
        )
        await self.send_reply(event, msg)
    
    async def cmd_help(self, event: AstrMessageEvent, arg: str | None = None):
        """显示帮助"""
        await self.send_reply(event, format_help())
    
    async def cmd_files(self, event: AstrMessageEvent, arg: str | None = None):
        """浏览文件"""
        path = arg or "."
        files = await file_ops.list_files(
            path,
            binary=self._binary()
        )
        from .formatters import format_file_list
        await self.send_reply(event, format_file_list(files, path))
    
    async def cmd_abort(self, event: AstrMessageEvent, arg: str | None = None):
        """中断当前会话（通过发送 /stop）"""
        resolved = await self._resolve_session(event, arg)
        if not resolved:
            await self.send_reply(event, "⚠️ 未指定会话。")
            return
        
        session_id = resolved["session"]["id"]
        
        try:
            # 发送停止命令
            result = await chat(
                "/stop",
                session_id=session_id,
                timeout=30,
                binary=self._binary(),
                yolo=self._yolo(),
            )
            await self.send_reply(event, f"⏹️ 已中断会话 `{session_id[:16]}...`")
        except HermesCliError as e:
            await self.send_reply(event, format_error(str(e)))
    
    async def cmd_quick_send(self, event: AstrMessageEvent, text: str):
        """快捷发送（处理 > 前缀消息）"""
        await self.cmd_send(event, text)
    
    async def cmd_delete(self, event: AstrMessageEvent, arg: str | None = None):
        """删除指定会话"""
        resolved = await self._resolve_session(event, arg)
        if not resolved:
            await self.send_reply(event, "⚠️ 请提供要删除的会话序号或 ID。")
            return
        
        session_id = resolved["session"]["id"]
        ok, msg = await delete_session(session_id, binary=self._binary(), force=True)
        await self.send_reply(event, msg)
    
    async def cmd_clean(self, event: AstrMessageEvent, arg: str | None = None):
        """批量清理旧会话"""
        days = 90
        if arg:
            try:
                days = int(arg)
            except ValueError:
                pass
        ok, msg = await prune_sessions(older_than=days, binary=self._binary(), force=True)
        await self.send_reply(event, msg)
    
    # ── 审批方法 ─────────────────────────────────────
    
    async def cmd_pending(self, event: AstrMessageEvent, arg: str | None = None):
        """查看待审批请求"""
        window_id = event.unified_msg_origin
        text = self.plugin.pending_mgr.get_summary(window_id)
        await self.send_reply(event, text)
    
    async def cmd_approve(self, event: AstrMessageEvent, arg: str | None = None):
        """批准待审批请求"""
        window_id = event.unified_msg_origin
        
        if arg:
            # 批准单个
            try:
                index = int(arg.strip())
                ok = await self.plugin.pending_mgr.approve_single(window_id, index)
                if ok:
                    await self.send_reply(event, f"✅ 已批准请求 #{index}")
                else:
                    await self.send_reply(event, f"⚠️ 找不到序号 {index} 的待审批请求")
            except ValueError:
                await self.send_reply(event, "⚠️ 请提供序号，如 `/hermes allow 1`")
        else:
            # 全部批准
            result = await self.plugin.pending_mgr.approve_all(window_id)
            if result:
                await self.send_reply(event, result)
            else:
                await self.send_reply(event, "📭 没有待审批的请求")
    
    async def cmd_deny(self, event: AstrMessageEvent, arg: str | None = None):
        """拒绝待审批请求"""
        window_id = event.unified_msg_origin
        
        if arg:
            try:
                index = int(arg.strip())
                ok = await self.plugin.pending_mgr.deny_single(window_id, index)
                if ok:
                    await self.send_reply(event, f"❌ 已拒绝请求 #{index}")
                else:
                    await self.send_reply(event, f"⚠️ 找不到序号 {index} 的待审批请求")
            except ValueError:
                await self.send_reply(event, "⚠️ 请提供序号，如 `/hermes deny 1`")
        else:
            result = await self.plugin.pending_mgr.deny_all(window_id)
            if result:
                await self.send_reply(event, result)
            else:
                await self.send_reply(event, "📭 没有待审批的请求")
