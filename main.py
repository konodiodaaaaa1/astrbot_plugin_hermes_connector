"""
Hermes Connector AstrBot 插件入口
在 AstrBot 上注册 /hermes 指令组和 LLM 工具
- @filter.llm_tool() 用在插件类方法上，支持自然语言触发
"""

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain

from .hermes_cli_client import chat, list_sessions, check_health, HermesCliError
from .command_handlers import CommandHandlers
from .state_manager import StateManager
from .notification_manager import NotificationManager
from .pending_manager import PendingManager
from . import formatters


@register("astrbot_plugin_hermes_connector", "konodiodaaaaa1",
          "连接 Hermes Agent，在聊天平台上远程操控 Hermes 会话，随时随地 Agent",
          "1.1.0")
class HermesConnectorPlugin(Star):
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        self.state_mgr = StateManager()
        self.pending_mgr = PendingManager()
        self.notification_mgr = NotificationManager(context, self.state_mgr)
        self.cmd_handlers = CommandHandlers(self)
        self.quick_prefix = self.config.get("quick_prefix", ">")
        
        logger.info(f"Hermes Connector 已加载。快捷前缀: '{self.quick_prefix}'")
    
    async def initialize(self):
        """插件初始化"""
        logger.info("Hermes Connector 正在启动...")
        try:
            health = await check_health()
            self.state_mgr.set_hermes_health(health)
            logger.info(f"Hermes: {health['version']}" if health['ok'] else f"Hermes 异常: {health['error']}")
        except Exception as e:
            logger.warning(f"Hermes 健康检查失败: {e}")
        
        try:
            binary = self.config.get("hermes_command", "hermes")
            sessions = await list_sessions(binary=binary)
            self.state_mgr.update_sessions_cache(sessions)
            logger.info(f"已加载 {len(sessions)} 个 Hermes 会话")
        except Exception as e:
            logger.warning(f"预加载会话列表失败: {e}")
        
        logger.info("Hermes Connector 启动完成")
    
    async def terminate(self):
        pass
    
    # ── 辅助 ─────────────────────────────────────────
    
    async def _refresh_sessions(self):
        try:
            sessions = await list_sessions(binary=self.config.get("hermes_command", "hermes"))
            self.state_mgr.update_sessions_cache(sessions)
        except Exception:
            pass
    
    # ── 指令组 ───────────────────────────────────────
    
    @filter.command_group("hermes")
    def hermes(self):
        """Hermes Agent 远程控制"""
        pass
    
    # ── 指令：会话管理 ───────────────────────────────
    
    @hermes.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        await self.cmd_handlers.cmd_list(event, None)
    
    @hermes.command("list", alias={"ls"})
    async def cmd_list_all(self, event: AstrMessageEvent, all: str = ""):
        await self.cmd_handlers.cmd_list(event, all)
    
    @hermes.command("status", alias={"s"})
    async def cmd_status(self, event: AstrMessageEvent, session: str = ""):
        await self.cmd_handlers.cmd_status(event, session or None)
    
    @hermes.command("switch", alias={"sw"})
    async def cmd_switch(self, event: AstrMessageEvent, session: str = ""):
        await self.cmd_handlers.cmd_switch(event, session or None)
    
    @hermes.command("messages", alias={"msg"})
    async def cmd_messages(self, event: AstrMessageEvent, session: str = ""):
        await self.cmd_handlers.cmd_messages(event, session or None)
    
    @hermes.command("rename")
    async def cmd_rename(self, event: AstrMessageEvent, name: str = ""):
        await self.cmd_handlers.cmd_rename(event, name or None)
    
    # ── 指令：消息发送 ───────────────────────────────
    
    @hermes.command("to")
    async def cmd_to(self, event: AstrMessageEvent, target: str = ""):
        await self.cmd_handlers.cmd_to(event, target or None)
    
    @hermes.command("send", alias={"say"})
    async def cmd_send(self, event: AstrMessageEvent, message: str = ""):
        await self.cmd_handlers.cmd_send(event, message)
    
    # ── 指令：会话创建 ───────────────────────────────
    
    @hermes.command("create", alias={"new"})
    async def cmd_create(self, event: AstrMessageEvent, prompt: str = ""):
        await self.cmd_handlers.cmd_create(event, prompt)
    
    # ── 指令：其他 ───────────────────────────────────
    
    @hermes.command("health", alias={"ping"})
    async def cmd_health(self, event: AstrMessageEvent):
        await self.cmd_handlers.cmd_health(event)
    
    @hermes.command("help")
    async def cmd_help(self, event: AstrMessageEvent, topic: str = ""):
        await self.cmd_handlers.cmd_help(event, topic or None)
    
    @hermes.command("files")
    async def cmd_files(self, event: AstrMessageEvent, path: str = ""):
        await self.cmd_handlers.cmd_files(event, path or None)
    
    @hermes.command("abort", alias={"stop"})
    async def cmd_abort(self, event: AstrMessageEvent, session: str = ""):
        await self.cmd_handlers.cmd_abort(event, session or None)
    
    # ── 审批指令 ─────────────────────────────────────
    
    @hermes.command("pending", alias={"p"})
    async def cmd_pending(self, event: AstrMessageEvent):
        """查看待审批请求"""
        await self.cmd_handlers.cmd_pending(event, None)
    
    @hermes.command("approve", alias={"a"})
    async def cmd_approve(self, event: AstrMessageEvent, index: str = ""):
        """批准待审批请求。不加序号则全部批准。"""
        await self.cmd_handlers.cmd_approve(event, index or None)
    
    @hermes.command("allow")
    async def cmd_allow(self, event: AstrMessageEvent, index: str = ""):
        """批准指定序号的请求"""
        await self.cmd_handlers.cmd_approve(event, index or None)
    
    @hermes.command("deny", alias={"d", "reject"})
    async def cmd_deny(self, event: AstrMessageEvent, index: str = ""):
        """拒绝待审批请求。不加序号则全部拒绝。"""
        await self.cmd_handlers.cmd_deny(event, index or None)
    
    # ── 快捷前缀 ─────────────────────────────────────
    
    @filter.regex(r"^(\d+)?\s*(.+)$")
    async def quick_send(self, event: AstrMessageEvent):
        text = event.get_message_str().strip()
        if not text.startswith(self.quick_prefix):
            return
        content = text[len(self.quick_prefix):].strip()
        if not content:
            return
        
        import re
        m = re.match(r"^(\d+)\s+(.+)$", content)
        if m:
            idx = int(m.group(1))
            msg = m.group(2)
            await self._refresh_sessions()
            resolved = await self.cmd_handlers._resolve_session(event, str(idx))
            if resolved:
                self.state_mgr.set_current_session(
                    event.get_sender_id(), resolved["session"]["id"], idx
                )
        
        await self.cmd_handlers.cmd_quick_send(event, content)
    
    # ═══════════════════════════════════════════════════
    #  LLM Function Calling 工具（自然语言触发）
    # ═══════════════════════════════════════════════════
    
    @filter.llm_tool(name="hermes_list_sessions")
    async def tool_list_sessions(self, event: AstrMessageEvent):
        """列出所有 Hermes Agent 会话及其当前状态。调用此工具后，用户可以指定序号来切换或发送消息。"""
        await self._refresh_sessions()
        sessions = self.state_mgr.get_sessions_cache()
        if not sessions:
            yield "当前没有 Hermes 会话。你可以说「创建会话」来新建一个。"
            return
        yield formatters.format_session_list(sessions)
    
    @filter.llm_tool(name="hermes_send_message")
    async def tool_send_message(self, event: AstrMessageEvent, message: str, session_idx: int = 1):
        """向指定的 Hermes Agent 会话发送消息。

        Args:
            message(string): 要发送的消息内容
            session_idx(number): 会话序号（从 1 开始），使用 hermes_list_sessions 查看
        """
        # 请求审批（如果配置开启）
        require_approval = self.config.get("require_approval", True)
        if require_approval:
            window_id = event.get_sender_id()
            approved, reason = await self.pending_mgr.require_approval(
                window_id, "hermes_send_message",
                {"message": message[:50], "session_idx": session_idx},
                lambda text: event.send(MessageChain(chain=[Plain(text)])),
                timeout=self.config.get("approval_timeout", 60)
            )
            if not approved:
                if reason == "timeout":
                    yield "操作超时：60秒内未收到用户审批。请使用 `/hermes a` 批准或 `/hermes deny` 拒绝。"
                elif reason == "notification_failed":
                    yield "操作失败：无法发送审批通知。请检查插件配置。"
                else:
                    yield "操作已被用户拒绝。"
                return
        
        await self._refresh_sessions()
        session = self.state_mgr.get_session_by_idx(session_idx)
        if not session:
            yield f"找不到序号 {session_idx} 的会话。请先调用 hermes_list_sessions 查看当前会话。"
            return
        
        yolo_mode = self.config.get("hermes_approval_mode", "normal") == "yolo"
        try:
            result = await chat(message, session_id=session["id"], timeout=120, yolo=yolo_mode)
            self.state_mgr.set_current_session(event.get_sender_id(), result["session_id"], session_idx)
            yield formatters.format_response(result["session_id"], result["response"])
        except HermesCliError as e:
            yield str(e)
    
    @filter.llm_tool(name="hermes_create_session")
    async def tool_create_session(self, event: AstrMessageEvent, prompt: str):
        """创建一个新的 Hermes Agent 会话，用于执行指定的任务。

        Args:
            prompt(string): 会话的初始任务描述或提示词
        """
        # 请求审批
        window_id = event.get_sender_id()
        approved, reason = await self.pending_mgr.require_approval(
            window_id, "hermes_create_session",
            {"prompt": prompt[:50]},
            lambda text: event.send(MessageChain(chain=[Plain(text)])),
            timeout=60
        )
        if not approved:
            if reason == "timeout":
                yield "操作超时：60秒内未收到用户审批。请使用 `/hermes a` 批准或 `/hermes deny` 拒绝。"
            elif reason == "notification_failed":
                yield "操作失败：无法发送审批通知。"
            else:
                yield "操作已被用户拒绝。"
            return
        
        yolo_mode = self.config.get("hermes_approval_mode", "normal") == "yolo"
        try:
            result = await chat(prompt, timeout=120, yolo=yolo_mode)
            self.state_mgr.set_current_session(event.get_sender_id(), result["session_id"])
            await self._refresh_sessions()
            yield formatters.format_response(result["session_id"], result["response"], is_new=True)
        except HermesCliError as e:
            yield str(e)
    
    @filter.llm_tool(name="hermes_switch_session")
    async def tool_switch_session(self, event: AstrMessageEvent, target: str):
        """切换到指定的 Hermes 会话。

        Args:
            target(string): 会话序号（如"1"）或会话 ID 前缀
        """
        await self._refresh_sessions()
        sessions = self.state_mgr.get_sessions_cache()
        
        # 解析序号
        try:
            idx = int(target)
            session = self.state_mgr.get_session_by_idx(idx)
            if not session:
                yield f"找不到序号 {idx} 的会话。当前共 {len(sessions)} 个会话。"
                return
        except ValueError:
            session = self.state_mgr.find_session_by_id_prefix(target)
            if not session:
                yield f"找不到 ID 前缀 '{target}' 的会话。"
                return
            idx = None
        
        self.state_mgr.set_current_session(event.get_sender_id(), session["id"], idx)
        preview = session.get("preview") or "无预览"
        title = session.get("title") or "未命名"
        yield f"已切换到会话 [{session['id'][:12]}...] {title} - {preview}"
    
    @filter.llm_tool(name="hermes_check_health")
    async def tool_check_health(self, event: AstrMessageEvent):
        """检查 Hermes Agent 的连接状态和版本信息。当用户报告 Hermes 不响应时优先调用此工具。"""
        try:
            health = await check_health()
            if health["ok"]:
                yield f"Hermes Agent 连接正常。版本: {health['version']}"
            else:
                yield f"Hermes Agent 连接异常: {health['error']}"
        except Exception as e:
            yield f"检查失败: {e}"
