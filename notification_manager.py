"""
通知管理器
将 Hermes 事件推送到 AstrBot 聊天窗口（预留扩展）
"""

import logging

logger = logging.getLogger("astrbot")


class NotificationManager:
    """管理通知推送和消息分发"""

    def __init__(self, context, state_mgr):
        self.context = context
        self.state_mgr = state_mgr

    async def push_notification(self, text: str, session_id: str | None = None) -> None:
        """推送通知到合适的聊天窗口。当前仅记录日志，避免依赖 AstrBot 私有属性。"""
        if not text:
            return
        logger.debug(f"[Hermes 通知] session={session_id}: {text[:100]}...")
