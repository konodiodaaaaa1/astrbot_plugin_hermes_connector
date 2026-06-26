# Hermes Hub

Hermes Hub 是 `astrbot_plugin_hermes_connector` 的远程后端，运行在 Hermes 所在的服务器上，通过 REST API + SSE 为 AstrBot 插件提供远程调用能力。

> 本分支基于 [konodiodaaaaa1/astrbot_plugin_hermes_connector](https://github.com/konodiodaaaaa1/astrbot_plugin_hermes_connector) 原版，并参考 [LiJinHao999/astrbot_plugin_hapi_connector](https://github.com/LiJinHao999/astrbot_plugin_hapi_connector) 的 HTTP/SSE Hub 架构改造而来。

## 快速开始

### 1. 准备 Hermes

确保服务器上已安装 Hermes CLI，或者已经有一个 Docker 容器在运行。

```bash
# 原生安装
hermes --version

# 或 Docker
docker run -d --name hermes -v hermes-data:/opt/data nousresearch/hermes-agent:latest sleep infinity
```

你当前复用 1Panel 创建的容器 `<hermes-container-name>`（例如 `hermes`），安装 Hub 时通过 `HERMES_CONTAINER` 指定即可。

### 2. 安装 Hub

从本仓库 Releases 下载 `hermes-hub.tar.gz`：

```bash
curl -L -o /tmp/hermes-hub.tar.gz \
  https://github.com/ziyue67/astrbot_plugin_hermes_connector/releases/download/v1.3.0/hermes-hub.tar.gz
sudo mkdir -p /opt/hermes-hub
sudo tar -xzf /tmp/hermes-hub.tar.gz -C /opt/hermes-hub
sudo bash /opt/hermes-hub/install.sh
```

默认监听 `127.0.0.1:9800`。脚本会自动生成 `HERMES_ACCESS_TOKEN` 并写入 `/etc/default/hermes-hub`。

### 3. 暴露到公网（必须加 HTTPS）

推荐方式：

- **直接 HTTPS**：安装时传入证书路径，Hub 会自己监听带 SSL 的端口。
  ```bash
  sudo HERMES_HOST=0.0.0.0 HERMES_PORT=9443 \
    HERMES_SSL_KEYFILE=/path/to/your.key \
    HERMES_SSL_CERTFILE=/path/to/your.pem \
    bash /opt/hermes-hub/install.sh
  sudo ufw allow 9443/tcp   # 或你的防火墙/安全组
  ```
- **Nginx / Caddy / OpenResty 反向代理**：让 Hub 继续监听 `127.0.0.1:9800`，由前端提供 HTTPS。
- **Cloudflare Tunnel / Tailscale**：不改变 Hub 监听地址，走隧道或内网 IP。

不要直接以 HTTP 形式暴露在公网。

**SSE / 长连接反代优化**

如果通过 Nginx / OpenResty / Caddy 反代 Hub，建议在站点配置中加入以下参数，避免长任务触发 504 Gateway Time-out 或 SSE 被缓冲：

```nginx
location ^~ / {
    proxy_pass http://127.0.0.1:9800;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $http_connection;

    # 长任务/后台轮询需要较长的超时
    proxy_connect_timeout 60s;
    proxy_send_timeout 600s;
    proxy_read_timeout 600s;

    # 必须关闭缓冲，否则 SSE 事件会被缓存，导致 TransferEncodingError
    proxy_buffering off;
    proxy_cache off;
}
```



### 4. 获取 Access Token

```bash
cat /etc/default/hermes-hub | grep HERMES_ACCESS_TOKEN
```

### 5. 配置 AstrBot 插件

- `remote_mode` → `hub`
- `hub_endpoint` → 你的 HTTPS 地址，例如 `https://your-server:9443`
- `access_token` → `/etc/default/hermes-hub` 中的 Token
- `hub_verify_ssl` → 使用 IP 或自签名证书时建议关闭（默认 `false`）

发送 `/hermes health` 验证连接。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `HERMES_ACCESS_TOKEN` | 插件连接用的 Access Token | 自动生成 |
| `HERMES_JWT_SECRET` | JWT 签名密钥 | 自动生成 |
| `HERMES_HOST` | 监听地址 | `127.0.0.1` |
| `HERMES_PORT` | 监听端口 | `9800` |
| `HERMES_SSL_KEYFILE` | HTTPS 私钥路径（可选） | 空 |
| `HERMES_SSL_CERTFILE` | HTTPS 证书路径（可选） | 空 |
| `HERMES_BINARY` | Hermes CLI 路径 | `hermes` |
| `HERMES_CONTAINER` | Docker 容器名，设为空则使用本地 Hermes | 空 |
| `HERMES_CORS_ORIGINS` | CORS 来源，逗号分隔 | 空 |

## 更新

安装后 `/opt/hermes-hub` 会自带两个手动安全更新脚本：

### 更新 Hermes Hub

```bash
sudo bash /opt/hermes-hub/update-hub.sh
```

脚本会：
1. 备份当前 `/opt/hermes-hub`
2. 下载最新 `hermes-hub.tar.gz`
3. 运行 `install.sh`，自动继承现有 Token、SSL 配置和容器名
4. 重启 `hermes-hub` 服务

### 更新 Hermes Agent（Docker/Compose）

```bash
# 先 dry-run
sudo python3 /opt/hermes-hub/update-hermes.py <容器名> --dry-run

# 正式执行（自动备份、失败回滚）
sudo python3 /opt/hermes-hub/update-hermes.py <容器名> --yes
```

- 若容器由 `docker-compose` 管理（如 1Panel 应用），脚本会修改 `docker-compose.yml` 里的镜像标签并 `docker compose up -d`。
- 若是普通 `docker run` 容器，脚本会根据 `docker inspect` 生成等价命令重建容器。
- 升级失败时会自动恢复到原镜像和原 compose 文件。

## API 端点

- `GET /health`
- `POST /api/auth`
- `GET /api/events`（SSE）
- `GET /api/sessions`
- `POST /api/sessions`
- `GET /api/sessions/{id}`
- `GET /api/sessions/{id}/messages`
- `POST /api/sessions/{id}/messages`
- `POST /api/sessions/{id}/stop`
- `POST /api/sessions/{id}/rename`
- `DELETE /api/sessions/{id}`
- `POST /api/sessions/prune`

## 致谢

- 原版插件：[konodiodaaaaa1/astrbot_plugin_hermes_connector](https://github.com/konodiodaaaaa1/astrbot_plugin_hermes_connector)
- 架构参考：[LiJinHao999/astrbot_plugin_hapi_connector](https://github.com/LiJinHao999/astrbot_plugin_hapi_connector)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) / [AstrBot](https://github.com/Soulter/AstrBot)
