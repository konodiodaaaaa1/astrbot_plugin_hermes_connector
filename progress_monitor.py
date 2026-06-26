"""
后台进度监控
轮询 Hermes 会话状态，在 token 阈值或任务完成时推送 LLM 生成的总结
"""

import asyncio
import json
import logging
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.event import MessageChain
from astrbot.core.platform import Platform
from astrbot.core.platform.message_session import MessageSession

from .hermes_cli_client import get_session_detail, get_session_messages, HermesCliError

logger = logging.getLogger("astrbot")


class ProgressBar:
    """单个会话的后台监控状态"""

    def __init__(self, session_id: str, started_at: float):
        self.session_id = session_id
        self.started_at = started_at
        self.last_token_count: int = 0
        self.last_message_count: int = 0
        self.last_poll_at: float = started_at
        self.reported_thresholds: set[int] = set()
        self.last_activity_at: float = started_at
        self.finished: bool = False
        self.task: asyncio.Task | None = None
        # 用于去重：上次汇报时的消息数
        self.last_reported_msg_count: int = 0


class ProgressMonitor:
    """管理所有活跃会话的后台进度监控"""

    def __init__(self, context, config):
        self.context = context
        self.config = config
        self._bars: dict[str, ProgressBar] = {}
        # session_id → {platform_id, session, umo} 用于推送
        self._push_targets: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return self.config.get("progress_monitor", True)

    @property
    def poll_interval(self) -> int:
        return self.config.get("progress_poll_interval", 30)

    @property
    def token_threshold(self) -> int:
        return self.config.get("progress_token_threshold", 100000)

    @property
    def idle_heartbeat(self) -> int:
        return self.config.get("progress_idle_heartbeat", 120)

    def register(self, session_id: str, event) -> None:
        """注册一个会话的后台监控目标"""
        real_event = event
        # ContextWrapper 兼容
        if hasattr(event, 'context') and hasattr(event.context, 'event') and not hasattr(event, 'unified_msg_origin'):
            real_event = event.context.event

        umo = real_event.unified_msg_origin
        platform_id = real_event.get_platform_id()
        session = real_event.session

        self._push_targets[session_id] = {
            "platform_id": platform_id,
            "session": session,
            "umo": umo,
        }

        bar = ProgressBar(session_id=session_id, started_at=time.time())
        self._bars[session_id] = bar

    def start_monitoring(self, session_id: str, event) -> asyncio.Task:
        """启动后台监控任务"""
        self.register(session_id, event)
        bar = self._bars[session_id]
        bar.task = asyncio.create_task(self._monitor_loop(session_id))
        logger.info(f"进度监控已启动: {session_id[:16]}... (轮询间隔={self.poll_interval}s, token阈值={self.token_threshold})")
        return bar.task

    def is_monitored(self, session_id: str) -> bool:
        return session_id in self._bars and not self._bars[session_id].finished

    def stop_monitoring(self, session_id: str) -> None:
        """停止监控某个会话"""
        bar = self._bars.get(session_id)
        if bar and bar.task and not bar.task.done():
            bar.task.cancel()
        if session_id in self._bars:
            self._bars[session_id].finished = True

    def stop_all(self) -> None:
        """停止所有监控"""
        for sid in list(self._bars.keys()):
            self.stop_monitoring(sid)

    async def _monitor_loop(self, session_id: str) -> None:
        """后台轮询循环"""
        bar = self._bars[session_id]
        binary = self.config.get("hermes_command", "hermes")
        poll_interval = self.poll_interval
        poll_count = 0

        try:
            while not bar.finished:
                await asyncio.sleep(poll_interval)
                poll_count += 1

                # 轮询会话状态
                detail = await self._poll_session(session_id, binary)
                if detail is None:
                    logger.debug(f"轮询 #{poll_count} {session_id[:16]}... 导出失败，跳过")
                    continue

                # 更新进度
                total_tokens = (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
                msg_count = detail.get("message_count") or 0
                ended_at = detail.get("ended_at")

                # 检测是否有新活动
                if msg_count > bar.last_message_count:
                    bar.last_activity_at = time.time()
                    bar.last_message_count = msg_count

                bar.last_token_count = total_tokens
                bar.last_poll_at = time.time()

                logger.debug(
                    f"轮询 #{poll_count} {session_id[:16]}... "
                    f"tokens={total_tokens:,} msgs={msg_count} ended={ended_at is not None}"
                )

                # 检查是否已完成
                if ended_at is not None:
                    bar.finished = True
                    logger.info(f"检测到会话完成: {session_id[:16]}... (轮询 #{poll_count})")
                    # 推送完成通知
                    await self._push_completion(session_id, detail)
                    break

                # 检查 token 阈值
                current_threshold = (total_tokens // self.token_threshold) * self.token_threshold
                if (current_threshold >= self.token_threshold
                        and current_threshold not in bar.reported_thresholds):
                    bar.reported_thresholds.add(current_threshold)
                    logger.info(f"触发 token 阈值汇报: {session_id[:16]}... (阈值={current_threshold:,})")
                    await self._push_threshold_summary(session_id, detail)
                    bar.last_reported_msg_count = msg_count
                    continue

                # 检查空闲心跳
                idle_seconds = time.time() - bar.last_activity_at
                if idle_seconds >= self.idle_heartbeat and msg_count == bar.last_reported_msg_count:
                    bar.last_reported_msg_count = msg_count  # 避免重复心跳
                    logger.info(f"触发空闲心跳: {session_id[:16]}... (空闲={int(idle_seconds)}s)")
                    await self._push_heartbeat(session_id, detail, idle_seconds)

        except asyncio.CancelledError:
            logger.debug(f"进度监控已取消: {session_id[:16]}... (共轮询 {poll_count} 次)")
        except Exception as e:
            logger.warning(f"进度监控异常 ({session_id[:16]}...): {e}", exc_info=True)

    async def _poll_session(self, session_id: str, binary: str) -> dict | None:
        """轮询 Hermes 会话状态（轻量：只取 session 级字段）"""
        try:
            code, stdout, stderr = await _run_hermes(
                ["sessions", "export", "--session-id", session_id, "-"],
                timeout=30, binary=binary
            )
            if code != 0:
                return None
            for line in stdout.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    return data
                except json.JSONDecodeError:
                    continue
            return None
        except Exception as e:
            logger.debug(f"轮询会话 {session_id} 失败: {e}")
            return None

    async def _get_recent_messages_text(self, session_id: str, binary: str, count: int = 5) -> str:
        """获取最近几条消息的文本片段"""
        try:
            messages = await get_session_messages(session_id, timeout=15, binary=binary)
            if not messages:
                return "(无消息)"
            recent = messages[-count:]
            lines = []
            for msg in recent:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
                content_preview = str(content)[:200]
                lines.append(f"[{role}]: {content_preview}")
            return "\n".join(lines)
        except Exception:
            return "(无法获取消息)"

    async def _generate_summary(self, detail: dict, recent_text: str) -> str:
        """调用 AstrBot 的 LLM 生成进度总结"""
        total_tokens = (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
        msg_count = detail.get("message_count", 0)
        tool_count = detail.get("tool_call_count", 0)
        title = detail.get("title") or "未命名"

        prompt = (
            f"你是 AI Agent 工作进度的观察者。以下是 Hermes Agent 当前的工作状态，"
            f"请用 2-3 句话向用户简明总结它的进展：\n\n"
            f"会话: {title}\n"
            f"Token: {total_tokens:,} (输入 {detail.get('input_tokens', 0):,} + 输出 {detail.get('output_tokens', 0):,})\n"
            f"消息数: {msg_count} | 工具调用: {tool_count} 次\n\n"
            f"最近活动片段:\n{recent_text}\n\n"
            f"请简洁总结当前进度，不要重复列出数字。"
        )

        try:
            umo = None
            # 尝试获取 provider
            for sid, target in self._push_targets.items():
                umo = target.get("umo")
                break
            provider = self.context.get_using_provider(umo)
            if provider is None:
                # fallback: 硬编码模板
                return f"📊 Token: {total_tokens:,} | 消息: {msg_count} | 工具调用: {tool_count} 次"

            resp = await provider.text_chat(prompt=prompt, system_prompt="你是一个简洁的进度汇报助手。")
            return resp.completion_text if hasattr(resp, "completion_text") else str(resp)
        except Exception as e:
            logger.debug(f"LLM 总结失败: {e}")
            return f"📊 Token: {total_tokens:,} | 消息: {msg_count} | 工具调用: {tool_count} 次"

    async def _push_to_chat(self, session_id: str, text: str) -> None:
        """推送消息到聊天窗口"""
        target = self._push_targets.get(session_id)
        if not target:
            return

        platform_id = target["platform_id"]
        session = target["session"]

        try:
            platform = self.context.get_platform_inst(platform_id)
            if platform is None:
                logger.warning(f"找不到平台实例: {platform_id}")
                return
            await platform.send_by_session(session, MessageChain(chain=[Plain(text)]))
        except Exception as e:
            logger.warning(f"推送进度通知失败: {e}")

    async def _push_threshold_summary(self, session_id: str, detail: dict) -> None:
        """Token 阈值汇报"""
        binary = self.config.get("hermes_command", "hermes")
        recent_text = await self._get_recent_messages_text(session_id, binary)
        summary = await self._generate_summary(detail, recent_text)
        elapsed = int(time.time() - self._bars[session_id].started_at)

        text = (
            f"📊 **Hermes 进度汇报**\n"
            f"会话: {session_id[:16]}...\n"
            f"{summary}\n\n"
            f"⏱️ 已运行: {elapsed // 60}m {elapsed % 60}s"
        )
        await self._push_to_chat(session_id, text)

    async def _push_heartbeat(self, session_id: str, detail: dict, idle_seconds: float) -> None:
        """空闲心跳"""
        elapsed = int(time.time() - self._bars[session_id].started_at)
        idle_min = int(idle_seconds // 60)
        total_tokens = (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
        msg_count = detail.get("message_count", 0)

        text = (
            f"⏳ **Hermes 心跳**\n"
            f"会话: {session_id[:16]}... 仍在运行\n"
            f"已运行: {elapsed // 60}m | 空闲: {idle_min}m\n"
            f"Token: {total_tokens:,} | 消息: {msg_count}"
        )
        await self._push_to_chat(session_id, text)

    async def _push_completion(self, session_id: str, detail: dict) -> None:
        """任务完成通知"""
        elapsed = int(time.time() - self._bars[session_id].started_at)
        total_tokens = (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
        msg_count = detail.get("message_count", 0)
        tool_count = detail.get("tool_call_count", 0)
        end_reason = detail.get("end_reason") or "completed"

        text = (
            f"✅ **Hermes 任务完成**\n"
            f"会话: {session_id[:16]}...\n"
            f"结束原因: {end_reason}\n"
            f"⏱️ 总耗时: {elapsed // 60}m {elapsed % 60}s\n"
            f"Token: {total_tokens:,} | 消息: {msg_count} | 工具调用: {tool_count}"
        )
        await self._push_to_chat(session_id, text)

        # 清理
        self.stop_monitoring(session_id)
