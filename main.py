"""
Hermes Connector AstrBot 插件入口
在 AstrBot 上注册 /hermes 指令组和 LLM 工具
- @filter.llm_tool() 用在插件类方法上，支持自然语言触发
"""

import asyncio

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain, Poke

from .hermes_cli_client import chat, list_sessions, check_health, get_session_detail, get_session_messages, delete_session, prune_sessions, rename_session_cmd, HermesCliError
from . import hermes_cli_client as _hcc
from .hermes_cli_client import subscribe_events
from .command_handlers import CommandHandlers
from .state_manager import StateManager
from .notification_manager import NotificationManager
from .pending_manager import PendingManager
from .risk_checker import classify_risk, get_risk_summary
from . import file_ops
from . import formatters
from .progress_monitor import ProgressMonitor


def _approval_failed_msg(reason: str) -> str:
    """审批失败/超时的统一提示"""
    if reason == "timeout":
        return "⏱️ 操作超时：未收到审批。请使用 `/hermes a` 批准或 `/hermes deny` 拒绝。"
    elif reason == "notification_failed":
        return "❌ 操作失败：无法发送审批通知。请检查插件配置。"
    elif reason == "cancelled":
        return "⏹️ 操作已取消。"
    return "⛔ 操作已被用户拒绝。"


def _safe_window_id(event) -> str:
    """从 event 安全获取窗口ID。

    指令处理器传入的是 AstrMessageEvent;
    LLM 工具在 AstrBot v4.26+ 中传入的是 ContextWrapper，
    需要通过 .context.event 提取实际的 AstrMessageEvent。
    """
    real_event = _safe_event(event)
    if hasattr(real_event, 'unified_msg_origin'):
        return real_event.unified_msg_origin
    return "internal_agent"


def _safe_event(event):
    """从 event 提取真正的 AstrMessageEvent。

    AstrBot v4.26+ 的 _PermissionGuardedTool 会将 LLM 工具的第一个参数
    从 AstrMessageEvent 替换为 ContextWrapper[AstrAgentContext]。
    ContextWrapper 的结构: .context.event → AstrMessageEvent
    """
    if hasattr(event, 'unified_msg_origin'):
        return event
    if hasattr(event, 'context') and hasattr(event.context, 'event'):
        return event.context.event
    return event


def _safe_set_session(state_mgr, event, session_id, idx=None):
    """安全设置当前会话。兼容两种 event 类型"""
    window_id = _safe_window_id(event)
    if idx:
        state_mgr.set_current_session(window_id, session_id, idx)
    else:
        state_mgr.set_current_session(window_id, session_id)


@register("astrbot_plugin_hermes_connector", "konodiodaaaaa1",
          "连接 Hermes Agent，在聊天平台上远程操控 Hermes 会话，随时随地 Agent",
          "1.3.3")
class HermesConnectorPlugin(Star):
    
    async def _hub_event_listener(self):
        """订阅 Hermes Hub 的 SSE 事件，带自动重连与事件分发。"""
        while True:
            try:
                async for event, data in subscribe_events(sse_timeout=90):
                    logger.debug(f"Hermes Hub event: {event} {data}")
                    await self._dispatch_hub_event(event, data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Hub 事件监听异常，5 秒后重连: {e}")
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise

    async def _dispatch_hub_event(self, event: str, data: dict):
        """根据 SSE 事件类型触发插件侧状态同步。"""
        if event in ("session_created", "session_deleted", "sessions_pruned"):
            try:
                await self._refresh_sessions()
            except Exception:
                pass
        elif event == "session_stopped" and data:
            sid = data.get("session_id")
            if sid and self.progress_monitor.is_monitored(sid):
                self.progress_monitor.stop_monitoring(sid)
        elif event in ("message_sent", "session_renamed"):
            try:
                await self._refresh_sessions()
            except Exception:
                pass

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        self.state_mgr = StateManager()
        self.pending_mgr = PendingManager()
        self.notification_mgr = NotificationManager(context, self.state_mgr)
        self.cmd_handlers = CommandHandlers(self)
        self.quick_prefix = self.config.get("quick_prefix", ">")
        self.poke_approve = self.config.get("poke_approve", True)
        self.progress_monitor = ProgressMonitor(context, config)
        # 持有后台任务引用，防止被 GC 回收
        self._bg_tasks: set[asyncio.Task] = set()

        # 根据配置选择本地 CLI 还是远程 Hub
        _hcc.configure_service(self.config)
        if _hcc.is_hub_mode():
            self._track_bg_task(asyncio.create_task(self._hub_event_listener()))
        
        logger.info(f"Hermes Connector 已加载。快捷前缀: '{self.quick_prefix}'")
    
    def _track_bg_task(self, task: asyncio.Task) -> None:
        """持有后台任务引用防止 GC，并在完成时记录异常"""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_task_done)
    
    def _bg_task_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc:
                logger.warning(f"后台任务异常: {exc}", exc_info=exc)
    
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
        self.progress_monitor.stop_all()
    
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
    
    @hermes.command("list", alias={"ls"})
    async def cmd_list(self, event: AstrMessageEvent, all: str = ""):
        await self.cmd_handlers.cmd_list(event, all or None)
    
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
    
    @hermes.command("delete", alias={"rm", "del"})
    async def cmd_delete(self, event: AstrMessageEvent, session: str = ""):
        """删除指定会话"""
        await self.cmd_handlers.cmd_delete(event, session or None)
    
    @hermes.command("clean", alias={"prune", "cleanup"})
    async def cmd_clean(self, event: AstrMessageEvent, days: str = ""):
        """批量清理旧会话"""
        await self.cmd_handlers.cmd_clean(event, days or None)
    
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
                _safe_set_session(self.state_mgr, event, resolved["session"]["id"], idx)
        
        await self.cmd_handlers.cmd_quick_send(event, content)
    
    # ── 戳一戳审批（QQ NapCat）───────────────────
    
    def _is_poke_event(self, event: AstrMessageEvent) -> bool:
        """检测是否为戳一戳机器人事件"""
        try:
            self_id = str(event.get_self_id() or "").strip()
            raw_message = getattr(event.message_obj, "raw_message", {}) or {}
            if not self_id:
                self_id = str(raw_message.get("self_id", "")).strip()
            
            for comp in getattr(event.message_obj, "message", []) or []:
                if isinstance(comp, Poke):
                    candidates = []
                    target_id = comp.target_id() if hasattr(comp, "target_id") else None
                    for value in (target_id, getattr(comp, "id", None), getattr(comp, "qq", None)):
                        if value is None:
                            continue
                        text = str(value).strip()
                        if text:
                            candidates.append(text)
                    if self_id and self_id in candidates:
                        return True
            
            subtype = str(raw_message.get("sub_type", "")).lower()
            target_id = str(raw_message.get("target_id", "")).strip()
            return subtype == "poke" and bool(self_id) and target_id == self_id
        except Exception:
            return False
    
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def poke_approve_handler(self, event: AstrMessageEvent):
        """戳一戳机器人 → 自动批准所有待审批请求 (仅 QQ NapCat)"""
        if not self.poke_approve:
            return
        
        if not self._is_poke_event(event):
            return
        
        window_id = _safe_window_id(event)
        items = self.pending_mgr.flatten_pending(window_id)
        if not items:
            return  # 无待审批，静默
        
        result = await self.pending_mgr.approve_all(window_id)
        if result:
            yield event.plain_result(f"[戳一戳审批] {result}")
        
        event.stop_event()
    
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
        # 智能审批
        approval_mode = self.config.get("require_approval", "smart")
        if not message or not message.strip():
            yield "⚠️ 消息内容为空，请输入要发送的消息。"
            return
        if approval_mode == "all":
            # 全部审批：每次都问
            window_id = _safe_window_id(event)
            approved, reason = await self.pending_mgr.require_approval(
                window_id, "hermes_send_message",
                {"message": message[:50], "session_idx": session_idx},
                lambda text: _safe_event(event).send(MessageChain(chain=[Plain(text)])),
                timeout=self.config.get("approval_timeout", 60)
            )
            if not approved:
                yield _approval_failed_msg(reason)
                return
        elif approval_mode == "smart":
            # 智能审批：AstrBot 判断风险，低风险自动放行
            risk = classify_risk(message)
            if risk == "high":
                window_id = _safe_window_id(event)
                risk_summary = get_risk_summary(message)
                approved, reason = await self.pending_mgr.require_approval(
                    window_id, "hermes_send_message",
                    {"risk": risk_summary, "message": message[:50], "session_idx": session_idx},
                    lambda text: _safe_event(event).send(MessageChain(chain=[Plain(text)])),
                    timeout=self.config.get("approval_timeout", 60)
                )
                if not approved:
                    yield _approval_failed_msg(reason)
                    return
            # medium 和 low 都自动放行
        
        await self._refresh_sessions()
        session = self.state_mgr.get_session_by_idx(session_idx)
        if not session:
            yield f"找不到序号 {session_idx} 的会话。请先调用 hermes_list_sessions 查看当前会话。"
            return
        
        yolo_mode = self.config.get("hermes_approval_mode", "normal") == "yolo"
        sid = session["id"]

        # 非阻塞模式：后台发送 + 进度监控
        if self.progress_monitor.enabled:
            # 立即返回，后台执行
            task = asyncio.create_task(
                self._background_chat(event, message, sid, session_idx)
            )
            self._track_bg_task(task)
            yield f"✅ 已提交到 Hermes 会话 [{sid[:16]}...]，后台执行中。我会在有进展时通知你。"
            return

        # 阻塞模式（fallback）
        try:
            result = await chat(
                message, session_id=sid,
                workdir=self.config.get("hermes_workdir", "") or None,
                model=self.config.get("hermes_model", "") or None,
                timeout=120, yolo=yolo_mode,
                binary=self.config.get("hermes_command", "hermes"),
            )
            _safe_set_session(self.state_mgr, event, result["session_id"], session_idx)

            # 自动汇报摘要
            response = result["response"]
            if self.config.get("auto_report", True):
                max_len = self.config.get("auto_report_max_length", 500)
                if len(response) > max_len:
                    response = response[:max_len] + f"\n\n...（回复过长，截断至 {max_len} 字符。可用 /hermes msg 查看完整消息）"

            yield formatters.format_response(result["session_id"], response)
        except HermesCliError as e:
            yield str(e)

    async def _background_chat(self, event, message: str, session_id: str, session_idx: int = 1):
        """后台发送消息给 Hermes + 启动进度监控

        使用 --quiet 模式启动 subprocess，等待自然结束（超时 30 分钟）。
        进度监控在并行轮询，subprocess 完成后从 session export 提取最终回复。
        """
        yolo_mode = self.config.get("hermes_approval_mode", "normal") == "yolo"
        binary = self.config.get("hermes_command", "hermes")

        # 启动进度监控
        monitor_task = self.progress_monitor.start_monitoring(session_id, event)

        try:
            # 后台执行 hermes chat，给予充足超时（最长 30 分钟，浏览器/研究类任务需要 10+ 分钟）
            result = await chat(
                message, session_id=session_id,
                workdir=self.config.get("hermes_workdir", "") or None,
                model=self.config.get("hermes_model", "") or None,
                timeout=1800, yolo=yolo_mode,
                binary=binary,
            )
            _safe_set_session(self.state_mgr, event, result["session_id"], session_idx)

            # 提取最终回复
            response = result["response"]
            if not response or response == "(无输出)":
                # subprocess 超时或无输出，从 session 提取最后的 assistant 回复
                from .hermes_cli_client import get_session_messages
                messages = await get_session_messages(
                    result["session_id"], timeout=30, binary=binary
                )
                if messages:
                    for msg in reversed(messages):
                        if msg.get("role") == "assistant":
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = " ".join([
                                    c.get("text", "") for c in content
                                    if isinstance(c, dict)
                                ])
                            response = str(content) or "(无回复)"
                            break

            max_len = self.config.get("auto_report_max_length", 1000)
            if len(response) > max_len:
                response = response[:max_len] + f"\n\n...（截断至 {max_len} 字符）"

            # 停止监控（避免重复完成通知）
            self.progress_monitor.stop_monitoring(session_id)

            final_text = (
                f"✅ **Hermes 任务完成**\n"
                f"会话: {result['session_id'][:16]}...\n\n"
                f"{response}"
            )
            await self._push_to_user(event, final_text)

        except HermesCliError as e:
            self.progress_monitor.stop_monitoring(session_id)
            # 超时也可能是任务还在 Hermes 后端跑，检查 session 是否有新回复
            err_str = str(e)
            if "超时" in err_str:
                await self._push_to_user(
                    event,
                    f"⏱️ Hermes 子进程超时，但会话可能仍在后端运行。\n"
                    f"会话: {session_id[:16]}...\n"
                    f"可用 `/hermes msg` 查看进度，或 `/hermes send 继续刚才的任务` 追问。"
                )
            else:
                await self._push_to_user(event, f"❌ Hermes 执行失败: {err_str[:300]}")
        except asyncio.CancelledError:
            self.progress_monitor.stop_monitoring(session_id)
        except Exception as e:
            self.progress_monitor.stop_monitoring(session_id)
            logger.warning(f"后台 chat 异常: {e}")

    async def _background_create(self, event, prompt: str):
        """后台创建新 Hermes 会话 + 进度监控

        新会话的 session_id 在 chat() 返回前是未知的，
        所以这里启动一个检测任务并行轮询 sessions list，
        发现新会话后立即 start_monitoring。
        """
        yolo_mode = self.config.get("hermes_approval_mode", "normal") == "yolo"
        binary = self.config.get("hermes_command", "hermes")

        # 获取现有会话 ID 集合，用于检测新会话
        existing_ids = set()
        try:
            sessions = await list_sessions(binary=binary)
            existing_ids = set(s["id"] for s in sessions)
        except Exception:
            pass

        # 启动新会话检测任务（独立于 chat 阻塞调用）
        async def _detect_new_session():
            for _ in range(24):  # 最多尝试 2 分钟
                await asyncio.sleep(5)
                try:
                    sessions = await list_sessions(binary=binary)
                    for s in sessions:
                        if s["id"] not in existing_ids:
                            self.progress_monitor.start_monitoring(s["id"], event)
                            logger.info(f"检测到新会话 {s['id'][:16]}...，已启动进度监控")
                            return s["id"]
                except Exception:
                    pass
            logger.warning("新会话检测超时（2分钟内未发现新会话），进度监控未启动")
            return None

        detect_task = asyncio.create_task(_detect_new_session())

        try:
            # 创建新会话（session_id=None），给予充足超时
            result = await chat(
                prompt,
                workdir=self.config.get("hermes_workdir", "") or None,
                model=self.config.get("hermes_model", "") or None,
                timeout=1800, yolo=yolo_mode,
                binary=binary,
            )
            sid = result["session_id"]
            _safe_set_session(self.state_mgr, event, sid)
            await self._refresh_sessions()

            # 停止检测任务
            if not detect_task.done():
                detect_task.cancel()
                try:
                    await detect_task
                except asyncio.CancelledError:
                    pass

            # 提取最终回复
            response = result["response"]
            if not response or response == "(无输出)":
                from .hermes_cli_client import get_session_messages
                messages = await get_session_messages(sid, timeout=30, binary=binary)
                if messages:
                    for msg in reversed(messages):
                        if msg.get("role") == "assistant":
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = " ".join([
                                    c.get("text", "") for c in content
                                    if isinstance(c, dict)
                                ])
                            response = str(content) or "(无回复)"
                            break

            max_len = self.config.get("auto_report_max_length", 1000)
            if len(response) > max_len:
                response = response[:max_len] + f"\n\n...（截断至 {max_len} 字符）"

            # 停止监控（避免重复完成通知）
            self.progress_monitor.stop_monitoring(sid)

            final_text = (
                f"🆕 **Hermes 新会话已创建**\n"
                f"会话: {sid[:16]}...\n\n"
                f"{response}"
            )
            await self._push_to_user(event, final_text)

        except HermesCliError as e:
            if not detect_task.done():
                detect_task.cancel()
            err_str = str(e)
            if "超时" in err_str:
                await self._push_to_user(
                    event,
                    f"⏱️ Hermes 创建会话超时，但可能仍在后端运行。\n"
                    f"可用 `/hermes list` 查看是否已创建。"
                )
            else:
                await self._push_to_user(event, f"❌ Hermes 创建失败: {err_str[:300]}")
        except asyncio.CancelledError:
            if not detect_task.done():
                detect_task.cancel()
        except Exception as e:
            if not detect_task.done():
                detect_task.cancel()
            logger.warning(f"后台 create 异常: {e}")

    async def _push_to_user(self, event, text: str):
        """通过缓存的 platform + session 推送消息给用户"""
        real_event = _safe_event(event)
        umo = real_event.unified_msg_origin
        platform_id = real_event.get_platform_id()
        session = real_event.session

        try:
            platform = self.context.get_platform_inst(platform_id)
            if platform:
                await platform.send_by_session(session, MessageChain(chain=[Plain(text)]))
            else:
                logger.warning(f"推送失败：找不到平台 {platform_id}")
        except Exception as e:
            logger.warning(f"推送消息失败: {e}")

    @filter.llm_tool(name="hermes_create_session")
    async def tool_create_session(self, event, prompt: str):
        """创建一个新的 Hermes Agent 会话，用于执行指定的任务。

        Args:
            prompt(string): 会话的初始任务描述或提示词
        """
        # 智能审批：创建会话属于中风险（消耗配额），smart 模式下也需要审批
        approval_mode = self.config.get("require_approval", "smart")
        if approval_mode in ("all", "smart"):
            window_id = _safe_window_id(event)
            risk = classify_risk(prompt)
            if approval_mode == "all" or risk == "high":
                risk_summary = get_risk_summary(prompt) if risk == "high" else "🟡 创建新会话"
                approved, reason = await self.pending_mgr.require_approval(
                    window_id, "hermes_create_session",
                    {"risk": risk_summary, "prompt": prompt[:50]},
                    lambda text: _safe_event(event).send(MessageChain(chain=[Plain(text)])),
                    timeout=self.config.get("approval_timeout", 60)
                )
                if not approved:
                    yield _approval_failed_msg(reason)
                    return
        
        yolo_mode = self.config.get("hermes_approval_mode", "normal") == "yolo"

        # 非阻塞模式：后台发送 + 进度监控
        if self.progress_monitor.enabled:
            # 用 _background_create 统一处理：后台执行 + 监控 + 完成后推送
            # 创建新会话时 session_id=None，_background_create 会通过 chat() 获取新 sid
            task = asyncio.create_task(
                self._background_create(event, prompt)
            )
            self._track_bg_task(task)
            yield f"⏳ 正在创建 Hermes 新会话...后台执行中，有进展时会通知你。"
            return

        # 阻塞模式（fallback）
        try:
            result = await chat(
                prompt,
                workdir=self.config.get("hermes_workdir", "") or None,
                model=self.config.get("hermes_model", "") or None,
                timeout=120, yolo=yolo_mode,
                binary=self.config.get("hermes_command", "hermes"),
            )
            _safe_set_session(self.state_mgr, event, result["session_id"])
            await self._refresh_sessions()

            # 自动汇报摘要
            response = result["response"]
            if self.config.get("auto_report", True):
                max_len = self.config.get("auto_report_max_length", 500)
                if len(response) > max_len:
                    response = response[:max_len] + f"\n\n...（回复过长，截断至 {max_len} 字符。可用 /hermes msg 查看完整消息）"

            yield formatters.format_response(result["session_id"], response, is_new=True)
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
        
        _safe_set_session(self.state_mgr, event, session["id"], idx)
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
    
    @filter.llm_tool(name="hermes_get_session_status")
    async def tool_get_status(self, event: AstrMessageEvent, session_idx: int = 1):
        """查看指定 Hermes 会话的详细信息，包括模型、消息数、创建时间等。

        Args:
            session_idx(number): 会话序号（从 1 开始），使用 hermes_list_sessions 查看
        """
        await self._refresh_sessions()
        session = self.state_mgr.get_session_by_idx(session_idx)
        if not session:
            yield f"找不到序号 {session_idx} 的会话。请先调用 hermes_list_sessions 查看当前会话。"
            return
        
        detail = await get_session_detail(session["id"], binary=self.config.get("hermes_command", "hermes"))
        yield formatters.format_session_status(session["id"], detail)
    
    @filter.llm_tool(name="hermes_get_messages")
    async def tool_get_messages(self, event: AstrMessageEvent, session_idx: int = 1, rounds: int = 1):
        """查看指定 Hermes 会话的最近消息。

        Args:
            session_idx(number): 会话序号（从 1 开始）
            rounds(number): 查看最近几轮对话（默认 1 轮）
        """
        await self._refresh_sessions()
        session = self.state_mgr.get_session_by_idx(session_idx)
        if not session:
            yield f"找不到序号 {session_idx} 的会话。"
            return
        
        try:
            messages = await get_session_messages(session["id"], timeout=30, binary=self.config.get("hermes_command", "hermes"))
            if not messages:
                yield f"会话 [{session['id'][:12]}...] 暂无消息。"
                return
            
            recent = messages[-(rounds * 4):]  # 粗略估计每轮 4 条
            lines = [f"📜 **最近消息** ({session['id'][:12]}...)\n"]
            for msg in recent:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
                role_icon = {"user": "👤", "assistant": "🤖", "tool": "🔧"}.get(role, "❓")
                content_preview = str(content)[:200]
                lines.append(f"{role_icon} **{role}**: {content_preview}")
            yield "\n".join(lines)
        except Exception as e:
            yield f"获取消息失败: {e}"
    
    @filter.llm_tool(name="hermes_abort_session")
    async def tool_abort_session(self, event: AstrMessageEvent, session_idx: int = 1):
        """中断指定的 Hermes 会话。用于停止正在运行的任务。

        Args:
            session_idx(number): 会话序号（从 1 开始）
        """
        await self._refresh_sessions()
        session = self.state_mgr.get_session_by_idx(session_idx)
        if not session:
            yield f"找不到序号 {session_idx} 的会话。"
            return
        
        try:
            result = await chat("/stop", session_id=session["id"],
                               workdir=self.config.get("hermes_workdir", "") or None,
                               model=self.config.get("hermes_model", "") or None,
                               timeout=30,
                               binary=self.config.get("hermes_command", "hermes"),
                               yolo=self.config.get("hermes_approval_mode", "normal") == "yolo")
            yield f"⏹️ 已中断会话 [{session['id'][:12]}...]"
        except Exception as e:
            yield f"中断失败: {e}"
    
    @filter.llm_tool(name="hermes_list_files")
    async def tool_list_files(self, event: AstrMessageEvent, path: str = "."):
        """浏览 Hermes 工作目录中的文件。

        Args:
            path(string): 目录路径，默认为当前目录
        """
        files = await file_ops.list_files(path, binary=self.config.get("hermes_command", "hermes"))
        if not files:
            yield f"📁 `{path}`\n(空目录或无法访问)"
            return
        
        lines = [f"📁 **{path}**", ""]
        for f in files[:30]:
            lines.append(f"- {f}")
        if len(files) > 30:
            lines.append(f"\n…还有 {len(files) - 30} 个文件")
        yield "\n".join(lines)
    
    @filter.llm_tool(name="hermes_approve_all")
    async def tool_approve_all(self, event: AstrMessageEvent):
        """批准当前所有待审批的请求。当用户确认安全时使用此工具批量放行。"""
        window_id = _safe_window_id(event)
        result = await self.pending_mgr.approve_all(window_id)
        if result:
            yield result
        else:
            yield "📭 没有待审批的请求。"
    
    @filter.llm_tool(name="hermes_deny_all")
    async def tool_deny_all(self, event: AstrMessageEvent):
        """拒绝当前所有待审批的请求。"""
        window_id = _safe_window_id(event)
        result = await self.pending_mgr.deny_all(window_id)
        if result:
            yield result
        else:
            yield "📭 没有待审批的请求。"
    
    @filter.llm_tool(name="hermes_delete_session")
    async def tool_delete_session(self, event: AstrMessageEvent, session_idx: int):
        """删除指定的 Hermes 会话。注意：此操作不可恢复！

        Args:
            session_idx(number): 要删除的会话序号（从 1 开始）
        """
        await self._refresh_sessions()
        session = self.state_mgr.get_session_by_idx(session_idx)
        if not session:
            yield f"找不到序号 {session_idx} 的会话。"
            return
        
        ok, msg = await delete_session(
            session["id"],
            binary=self.config.get("hermes_command", "hermes"),
            force=True,
        )
        yield msg
    
    @filter.llm_tool(name="hermes_cleanup_sessions")
    async def tool_cleanup_sessions(self, event: AstrMessageEvent, older_than_days: int = 90):
        """批量清理旧的 Hermes 会话。删除指定天数之前的所有非活跃会话。

        Args:
            older_than_days(number): 删除超过多少天的旧会话（默认 90 天）
        """
        ok, msg = await prune_sessions(
            older_than=older_than_days,
            binary=self.config.get("hermes_command", "hermes"),
            force=True,
        )
        yield msg
    
    @filter.llm_tool(name="hermes_rename_session")
    async def tool_rename_session(self, event: AstrMessageEvent, new_title: str, session_idx: int = 1):
        """重命名 Hermes 会话，方便后续识别。

        Args:
            new_title(string): 会话的新名称
            session_idx(number): 会话序号（从 1 开始），默认为当前会话
        """
        await self._refresh_sessions()
        session = self.state_mgr.get_session_by_idx(session_idx)
        if not session:
            yield f"找不到序号 {session_idx} 的会话。"
            return
        
        ok, msg = await rename_session_cmd(
            session["id"], new_title,
            binary=self.config.get("hermes_command", "hermes"),
        )
        yield msg
