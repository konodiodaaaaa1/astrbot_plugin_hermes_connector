<div align="center">

# Hermes控制器

_✨ 在 QQ/微信/Telegram 上远程操控 Hermes Agent ✨_

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/GitHub-konodiodaaaaa1-blue)](https://github.com/konodiodaaaaa1/astrbot_plugin_hermes_connector)

</div>

---

## 📦 这是什么？

**Hermes控制器** 是一个 AstrBot 插件，让你在 **QQ、微信、Telegram** 等聊天平台上，远程操控你电脑上运行的 [Hermes Agent](https://github.com/NousResearch/hermes-agent)（由 Nous Research 开发的开源 AI Agent 框架）。

**一句话总结**：Hermes Agent 的远程遥控器。摸鱼时也能让 Hermes 继续干活。

## ✨ 功能

- **远程会话管理** — 创建、切换、查看 Hermes Agent 会话
- **远程发消息** — 在聊天窗口直接给 Hermes 下发任务
- **实时查看回复** — Hermes 返回的内容直接出现在你的聊天窗口
- **快捷发送** — 支持 `> 消息` 前缀快捷发送，无需打指令
- **自然语言操控** — 通过 AstrBot 的 LLM Function Calling，用自然语言管理 Hermes
- **会话持久化** — 所有会话通过 Hermes 内置 session 系统自动管理
- **文件浏览** — 远程查看 Hermes 工作目录文件

## 🔧 工作原理

```
你的聊天窗口         你的电脑（本地）
(QQ/微信/TG)         Hermes Agent 运行中
     │                     │
     ▼                     ▼
  AstrBot ──→ Hermes控制器插件 ──→ Hermes CLI (subprocess)
                                         │
                                    hermes chat -q "msg" --quiet
                                         │
                                    Hermes Agent
                                    (AI 处理)
```

插件通过 **subprocess** 调用本地 `hermes` CLI，使用 `--quiet` 模式获取纯净的输出，无需修改 Hermes 配置即可工作。

## 🚀 安装

### 前置条件

1. **Hermes Agent** 已安装并可正常运行
   ```bash
   hermes --version
   # 应显示版本号
   ```

2. **AstrBot** v3.4+ 已安装运行

### 方法一：插件市场安装（推荐）

在 AstrBot WebUI 插件市场搜索 **Hermes控制器**，一键安装。

### 方法二：手动安装

1. 下载 `astrbot_plugin_hermes_connector.zip`
2. 在 AstrBot WebUI → 插件管理 → 上传安装
3. 重启 AstrBot
4. 在插件配置页填写配置

## ⚙️ 配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `hermes_command` | Hermes CLI 命令路径 | `hermes` |
| `hermes_workdir` | Hermes 工作目录 | 空（使用 Hermes 默认） |
| `hermes_model` | 创建新会话默认模型 | 空（使用 Hermes 默认） |
| `max_timeout` | 命令超时时间（秒） | 120 |
| `quick_prefix` | 快捷发送前缀 | `>` |
| `output_mode` | 输出模式：`simple` / `verbose` | `simple` |
| `auto_create_session` | 快捷发送时自动创建新会话 | `true` |
| `default_system_prompt` | 新会话默认系统提示词 | 空（使用 Hermes 默认） |

## ⌨️ 指令

### 会话管理

| 指令 | 说明 |
|------|------|
| `/hermes list` | 查看所有会话 |
| `/hermes sw <序号或ID>` | 切换当前会话 |
| `/hermes status` | 查看当前会话状态 |
| `/hermes msg [轮数]` | 查看最近消息 |
| `/hermes create <提示词>` | 创建新会话 |
| `/hermes rename <名称>` | 重命名当前会话 |

### 消息发送

| 指令 | 说明 |
|------|------|
| `/hermes to <序号> <内容>` | 发送到指定会话 |
| `/hermes send <内容>` | 发送到当前会话 |
| `> 内容` | 快捷发送到当前会话 |
| `>N 内容` | 快捷发送到第 N 个会话 |

### 其他

| 指令 | 说明 |
|------|------|
| `/hermes health` | 检查 Hermes 连接 |
| `/hermes files <路径>` | 浏览文件 |
| `/hermes abort` | 中断当前会话 |
| `/hermes help` | 显示帮助 |

## 🧠 自然语言（LLM 工具）

在 AstrBot 管理面板开启工具的插件后，可以用自然语言操控：

| 你说 | 插件会 |
|------|--------|
| "帮我看看有哪些 Hermes 会话" | 调用 `hermes_list_sessions` 列出会话 |
| "给第 1 个会话发消息：继续优化代码" | 调用 `hermes_send_message` 发送消息 |
| "创建一个新会话，写个 Flask 应用" | 调用 `hermes_create_session` 创建会话 |
| "切换到第 2 个会话" | 调用 `hermes_switch_session` 切换 |
| "Hermes 还活着吗？" | 调用 `hermes_check_health` 检查状态 |

## 🎯 使用示例

```
👤 /hermes create 帮我写一个 Python 爬虫
🤖 ⏳ 正在创建 Hermes 新会话...
🤖 ✅ 已创建会话
   Session: 20260622_014252_...
   好的，我来写一个 Python 爬虫...

👤 /hermes to 1 加上异常处理
🤖 ⏳ 正在发送...
🤖 已添加异常处理...

👤 > 再加个 User-Agent 随机池
🤖 已添加 User-Agent 随机池...
```

## 🏗️ 插件结构

```
astrbot_plugin_hermes_connector/
├── main.py                  # 🎯 插件入口：指令组 + LLM 工具
├── hermes_cli_client.py     # 🔗 Hermes CLI 异步通信层
├── command_handlers.py      # 🎮 所有 /hermes 子命令
├── formatters.py            # 🎨 输出格式化
├── state_manager.py         # 📊 窗口会话状态管理
├── notification_manager.py  # 🔔 通知推送
├── file_ops.py              # 📁 文件操作
├── metadata.yaml            # 📋 插件元数据
├── _conf_schema.json        # ⚙️ 插件配置 schema
├── README.md                # 📖 本文档
└── docs/install.md          # 安装指南
```

## 📄 许可证

MIT

## 🙏 致谢

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — Nous Research 开源 AI Agent 框架
- [AstrBot](https://github.com/Soulter/AstrBot) — 跨平台聊天机器人框架
- [HAPI Vibe Coding 遥控器](https://github.com/LiJinHao999/astrbot_plugin_hapi_connector) — 本插件的设计参考
