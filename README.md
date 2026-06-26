# Hermes 控制器 (AstrBot 插件)

连接 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 与 [AstrBot](https://github.com/Soulter/AstrBot)，让你在 QQ、微信、Telegram 等任意聊天平台上直接操控 Hermes Agent 会话。

本插件支持两种运行模式：

- **本地模式**：AstrBot 与 Hermes 运行在同一台机器上，插件通过 subprocess 直接调用本地 `hermes` CLI。
- **Hub 远程模式**：AstrBot 通过 HTTPS + SSE 连接远端 **Hermes Hub**，Hermes 实际运行在远程服务器上。日常运行无需暴露 SSH，仅需暴露受 HTTPS 保护的 Hub 端口。

---

## 目录

- [功能特性](#功能特性)
- [架构概览](#架构概览)
- [安装插件](#安装插件)
- [模式一：本地模式（默认）](#模式一本地模式默认)
- [模式二：Hub 远程模式](#模式二hub-远程模式)
- [常用命令](#常用命令)
- [配置项说明](#配置项说明)
- [更新与维护](#更新与维护)
- [致谢](#致谢)

---

## 功能特性

- 💬 在聊天窗口直接与 Hermes Agent 对话
- 📋 列出、切换、创建、重命名、删除 Hermes 会话
- 🚀 快捷发送前缀（默认 `> `），无需输入完整命令
- 🧠 支持自定义默认模型和系统提示词
- ✅ 审批模式：`all` / `smart` / `off` 三档可选
- 📈 后台进度监控，自动汇报 token 消耗和执行摘要
- 🌐 远程模式：通过 Hermes Hub 把 Hermes 部署到独立服务器
- 🔒 Hub 模式使用 JWT 鉴权 + HTTPS，SSE 实时推送事件

---

## 架构概览

### 本地模式

```
QQ / 微信 / Telegram
        ↕
    AstrBot
        ↕
 Hermes Connector 插件
        ↕
  hermes CLI (subprocess)
        ↕
   Hermes Agent
```

### Hub 远程模式

```
QQ / 微信 / Telegram
        ↕
    AstrBot
        ↕
 Hermes Connector 插件
        ↕   HTTPS + SSE
   Hermes Hub (FastAPI)
        ↕   本地 subprocess / docker exec
   hermes CLI
        ↕
   Hermes Agent
```

---

## 安装插件

### 方式一：通过 GitHub 链接安装（推荐）

1. 打开 AstrBot 管理面板 → 插件 → 安装插件。
2. 输入仓库地址：
   ```
   https://github.com/ziyue67/astrbot_plugin_hermes_connector
   ```
3. 等待安装完成，启用插件并进入配置页。

### 方式二：手动安装

1. 从本仓库 [Releases](https://github.com/ziyue67/astrbot_plugin_hermes_connector/releases) 下载 `astrbot_plugin_hermes_connector.zip`。
2. 解压到 AstrBot 的 `data/plugins/` 目录下。
3. 重启 AstrBot，启用插件。

---

## 模式一：本地模式（默认）

适用场景：AstrBot 和 Hermes 装在同一台服务器或电脑上。

### 前置条件

- 已安装 Hermes CLI，并且可以在终端执行：
  ```bash
  hermes --version
  ```

### 配置

在插件配置页：

- `remote_mode` → `local`
- `hermes_command` → `hermes`（如果不在 PATH 中，填写绝对路径）
- 其他项保持默认即可

### 验证

在聊天窗口发送：

```
/hermes health
```

看到 Hermes 版本号即表示连接成功。

---

## 模式二：Hub 远程模式

适用场景：Hermes 运行在独立远程服务器，AstrBot 只通过 HTTPS 与 Hub 通信。

### 1. 在远程服务器部署 Hermes

选择以下任意一种方式：

**原生安装 Hermes CLI**

```bash
hermes --version
```

**使用 Docker 运行 Hermes**

```bash
docker run -d --name hermes \
  -v hermes-data:/opt/data \
  nousresearch/hermes-agent:latest \
  sleep infinity
```

如果你已有现成的 Hermes 容器（例如通过 1Panel 部署），记下容器名，后续安装 Hub 时通过 `HERMES_CONTAINER` 指定即可。

### 2. 部署 Hermes Hub

从 Releases 下载 `hermes-hub.tar.gz`，一键安装：

```bash
curl -L -o /tmp/hermes-hub.tar.gz \
  https://github.com/ziyue67/astrbot_plugin_hermes_connector/releases/download/v1.3.3/hermes-hub.tar.gz
sudo mkdir -p /opt/hermes-hub
sudo tar -xzf /tmp/hermes-hub.tar.gz -C /opt/hermes-hub
sudo bash /opt/hermes-hub/install.sh
```

更多细节（HTTPS、环境变量、systemd、升级脚本）请参考 [hub/README.md](./hub/README.md)。

### 3. 暴露到公网（必须 HTTPS）

推荐方式：

- Nginx / Caddy / OpenResty 反向代理
- Cloudflare Tunnel
- Tailscale / 内网穿透

**不要直接以 HTTP 形式暴露在公网。**

### 4. 配置 AstrBot 插件

在插件配置页：

- `remote_mode` → `hub`
- `hub_endpoint` → `https://your-hermes-hub.example.com`
- `access_token` → Hub 安装时生成的 Token（查看 `/etc/default/hermes-hub`）
- `hub_verify_ssl` → 使用正规证书开 `true`；IP/自签名证书建议关 `false`

### 5. 验证

```
/hermes health
```

看到远端 Hermes 版本号即表示远程连接成功。

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `/hermes help` | 显示帮助 |
| `/hermes health` | 检查 Hermes / Hub 连接状态 |
| `/hermes list` | 列出所有会话 |
| `/hermes status [序号]` | 查看指定会话状态 |
| `/hermes create <提示词>` | 创建新会话并开始任务 |
| `/hermes to <序号> <消息>` | 发送消息到指定会话 |
| `/hermes send <消息>` | 发送消息到当前会话 |
| `/hermes sw <序号>` | 切换当前会话 |
| `/hermes msg` | 查看当前会话最近消息 |
| `/hermes abort` | 中断当前会话执行 |
| `/hermes rename <新名字>` | 重命名当前会话 |
| `/hermes delete <序号>` | 删除指定会话 |
| `/hermes prune` | 清理已完成的会话 |
| `/hermes files [路径]` | 浏览文件目录 |
| `/hermes export [序号]` | 导出会话为 Markdown |
| `/hermes models` | 列出可用模型 |
| `/hermes config` | 查看当前配置 |

### 快捷发送

- `> 你好` → 发送到当前会话
- `>1 你好` → 发送到第 1 个会话
- 触发方式可在配置里通过 `quick_prefix` 修改

### 审批交互

当 Hermes 执行需要确认的操作时，机器人会发送待审批列表。你可以：

- 回复 `y` / `yes` / `确认` 批准全部
- 回复 `n` / `no` / `拒绝` 拒绝全部
- 回复序号（如 `1 3`）只批准第 1、3 条

---

## 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `remote_mode` | string | `local` | `local` 本地 CLI / `hub` 远程 Hub |
| `hub_endpoint` | string | 空 | Hub 地址，如 `https://your-hub.example.com` |
| `access_token` | string | 空 | Hub 鉴权 Token |
| `hub_timeout` | int | 120 | Hub 请求超时（秒） |
| `hub_verify_ssl` | bool | `false` | 是否验证 Hub HTTPS 证书 |
| `cf_access_client_id` | string | 空 | Cloudflare Access Client ID（可选） |
| `cf_access_client_secret` | string | 空 | Cloudflare Access Client Secret（可选） |
| `hermes_command` | string | `hermes` | 本地模式下 Hermes CLI 路径 |
| `hermes_workdir` | string | 空 | Hermes 工作目录 |
| `hermes_model` | string | 空 | 新会话默认模型 |
| `max_timeout` | int | 120 | 命令最大超时（秒） |
| `quick_prefix` | string | `>` | 快捷发送前缀 |
| `output_mode` | string | `simple` | `simple` 仅最终回复 / `verbose` 含思考过程 |
| `auto_create_session` | bool | `true` | 快捷前缀无会话时自动创建 |
| `default_system_prompt` | string | 空 | 新会话默认系统提示词 |
| `require_approval` | string | `smart` | `all` / `smart` / `off` |
| `poke_approve` | bool | `true` | 戳一戳自动审批全部（QQ NapCat） |
| `approval_timeout` | int | 60 | 审批超时时间（秒） |
| `auto_report` | bool | `true` | 任务完成自动汇报摘要 |
| `auto_report_max_length` | int | 500 | 汇报摘要最大长度 |
| `hermes_approval_mode` | string | `normal` | `normal` / `yolo`（谨慎使用） |
| `progress_monitor` | bool | `true` | 后台进度监控 |
| `progress_poll_interval` | int | 30 | 进度轮询间隔（秒） |
| `progress_token_threshold` | int | 100000 | 每消耗多少 token 汇报一次 |
| `progress_idle_heartbeat` | int | 120 | 空闲心跳间隔（秒） |

---

## 更新与维护

### 更新插件

在 AstrBot 插件管理页面点击更新，或重新下载 Release 中的 `astrbot_plugin_hermes_connector.zip` 覆盖安装。

### 更新 Hermes Hub

在 Hub 服务器执行：

```bash
sudo bash /opt/hermes-hub/update-hub.sh
```

### 更新 Hermes Agent（Docker）

```bash
sudo python3 /opt/hermes-hub/update-hermes.py <容器名> --yes
```

升级失败会自动回滚。更多细节见 [hub/README.md](./hub/README.md)。

---

## 致谢

- 原版插件：[konodiodaaaaa1/astrbot_plugin_hermes_connector](https://github.com/konodiodaaaaa1/astrbot_plugin_hermes_connector)
- Hub 架构参考：[LiJinHao999/astrbot_plugin_hapi_connector](https://github.com/LiJinHao999/astrbot_plugin_hapi_connector)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [AstrBot](https://github.com/Soulter/AstrBot)

---

## License

[MIT](./LICENSE)
