# 更新日志

## v1.3.3 — Hub 远程模式 + 稳定性修复

### Hub 远程模式（新增）

1. **新增 Hermes Hub 后端**：FastAPI + JWT 鉴权 + SSE 事件推送，让 AstrBot 可以通过 HTTPS 连接远程服务器上的 Hermes
   - 支持 Docker 容器模式（`docker exec`）和原生 CLI 模式
   - 自动生成 Access Token，JWT 密钥持久化到文件（重启不失效）
   - SSE 事件实时推送：session_created、message_sent、session_stopped 等

2. **新增插件端 Hub 客户端**：`AsyncHermesHubClient` + `AsyncTokenManager`
   - JWT 自动获取、提前刷新、401 自动重试
   - SSE 长连接带 `sock_read` 超时 + 自动重连（5 秒间隔）
   - SSE 事件分发：收到事件后自动刷新会话列表 / 停止进度监控

3. **local/hub 双模式抽象**：`HermesService` 基类 + `LocalHermesService` / `HubHermesService`
   - 原有调用方零改动，通过 `configure_service()` 切换模式
   - `remote_mode=local`（默认）完全保持原有行为

4. **Hub 部署工具**：
   - `install.sh`：一键安装到 `/opt/hermes-hub`，自动生成 systemd 服务
   - `update-hub.sh`：安全更新 Hub（备份 → 下载 → 安装 → 重启）
   - `update-hermes.py`：安全更新 Docker 容器（支持 compose / docker run，自动备份 + 失败回滚）
   - GitHub Actions 自动打包插件 zip 和 hub tar.gz 到 Releases

### 稳定性修复

1. 修复 `progress_monitor.py` 在 Hub 模式下 `_run_hermes` 未定义导致的 `NameError`，改用 `get_session_detail()` 服务抽象
2. 修复 `/api/auth` 返回非法 tuple `({"ok": False}, 401)` 导致客户端无法识别 401 的问题，改为 `raise HTTPException`
3. 修复 SSE 事件监听无重连机制的问题，断线后永久静默 → 改为 `while True` + 5 秒重连
4. 修复 `CancelledError` 在 CLI 调用和后台任务中被静默吞掉的问题，改为正确向上传播
5. Hub HTTP 客户端对非 JSON 响应、5xx 错误做了检查和一次性重试
6. 所有含 `{session_id}` 的 Hub 路由统一校验 ID 格式，防止命令注入
7. Hermes 输出解析前先 stripping ANSI 转义码，避免终端着色码干扰
8. `install.sh` 对 `/etc/default/hermes-hub` 设置 `chmod 600`，保护敏感凭据
9. `install.sh` 中 `run.sh` 和 service 文件的安装路径改为动态替换，支持非默认安装目录

### 清理与优化

1. 文件操作（`file_ops.py`）在本地模式下直接走文件系统，不再每次启动 Hermes 进程
2. 移除 `notification_manager.py` 对 AstrBot 私有 `_platform_manager` 属性的依赖
3. 降低 `risk_checker` 中 `token/secret/passwd` 等词的误报
4. 清理未使用的 import（`import time`、`import logging`、`Request`、`Field` 等）
5. 修复 `shlex` 和 `time` 在 `update-hermes.py` 中函数内 import 的顺序风险，移到文件顶部
6. 版本号统一：`main.py` @register 和 `metadata.yaml` 都为 `v1.3.3`
7. 合并 `hub/README.md` 到主 `README.md`，只保留一个文档入口
8. 所有 Release 下载 URL 统一使用原仓库，`update-hub.sh` 改为 `/releases/latest/download/` 自动获取最新版
9. 致谢区改为 @ziyue67 贡献者署名

---

## v1.2.5 — 后台进度监控 + 非阻塞发送

1. 新增后台进度监控：发送消息给 Hermes 后立即返回，后台轮询进度并定期推送 LLM 生成的总结
2. 修复轮询监控和主动汇报完全不触发的问题
3. 非阻塞超时从 30s 改为 1800s（浏览器/研究类任务需要 10+ 分钟）
4. 新增 `_background_create`：新会话创建时并行检测新会话 ID 并启动监控

---

## v1.2.0 — bug 修复

1. 修复 `ContextWrapper` 在 LLM 工具中的兼容性问题（AstrBot v4.26+）
2. 修复会话 ID 导出和 window ID 相关的 6 个关键 bug

---

## v1.1.0 — 审批系统 + LLM 工具

1. 新增智能审批（risk-based）：`all` / `smart` / `off` 三档可选
   - `smart` 模式下读取/搜索等低风险操作自动放行，删除/覆盖等高风险操作需要确认
2. 新增戳一戳自动审批（QQ NapCat）：戳一戳机器人自动批准所有待审批请求
3. 新增 11 个 LLM Function Calling 工具，支持自然语言管理 Hermes 会话
4. 新增任务完成自动汇报摘要
5. 新增会话删除、批量清理、重命名命令

---

## v1.0.0 — 初始版本

1. 连接 Hermes Agent 与 AstrBot，在 QQ、微信、Telegram 等聊天平台上远程操控 Hermes 会话
2. 支持会话列表、切换、创建、消息发送、状态查看
3. 快捷发送前缀（默认 `> `）
4. 配置项：模型、工作目录、超时、输出模式等