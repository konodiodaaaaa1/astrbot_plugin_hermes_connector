#!/usr/bin/env bash
# 在远程服务器上一键安装 Hermes Hub
set -e

INSTALL_DIR="${HERMES_HUB_DIR:-/opt/hermes-hub}"
SERVICE_NAME="hermes-hub"

if [ "$EUID" -ne 0 ]; then
  echo "请用 root 或 sudo 运行此脚本"
  exit 1
fi

echo "安装目录: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/hub"

# 将 Python 包文件安装到 hub/ 子目录
cp -f *.py "$INSTALL_DIR/hub/"
cp -f requirements.txt "$INSTALL_DIR/"
cp -f README.md "$INSTALL_DIR/"
cp -f hermes-hub.service "$INSTALL_DIR/"
cp -f update-*.sh "$INSTALL_DIR/" 2>/dev/null || true
cp -f update-*.py "$INSTALL_DIR/" 2>/dev/null || true

cd "$INSTALL_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 生成随机 access token 并写入 systemd 环境文件
ACCESS_TOKEN="${HERMES_ACCESS_TOKEN:-$(openssl rand -hex 32)}"
JWT_SECRET="${HERMES_JWT_SECRET:-$(openssl rand -hex 32)}"
cat > /etc/default/hermes-hub <<EOF
HERMES_ACCESS_TOKEN=$ACCESS_TOKEN
HERMES_JWT_SECRET=$JWT_SECRET
HERMES_HOST=${HERMES_HOST:-127.0.0.1}
HERMES_PORT=${HERMES_PORT:-9800}
HERMES_BINARY=${HERMES_BINARY:-hermes}
HERMES_CONTAINER=${HERMES_CONTAINER:-}
EOF
if [[ -n "${HERMES_SSL_KEYFILE:-}" ]]; then
  echo "HERMES_SSL_KEYFILE=${HERMES_SSL_KEYFILE}" >> /etc/default/hermes-hub
fi
if [[ -n "${HERMES_SSL_CERTFILE:-}" ]]; then
  echo "HERMES_SSL_CERTFILE=${HERMES_SSL_CERTFILE}" >> /etc/default/hermes-hub
fi

# 生成启动包装脚本（支持可选 HTTPS）
cat > "$INSTALL_DIR/run.sh" <<'RUNEOF'
#!/usr/bin/env bash
cd /opt/hermes-hub
. /etc/default/hermes-hub
ARGS=(--host "${HERMES_HOST}" --port "${HERMES_PORT}")
if [[ -n "${HERMES_SSL_KEYFILE:-}" && -n "${HERMES_SSL_CERTFILE:-}" ]]; then
  ARGS+=(--ssl-keyfile "${HERMES_SSL_KEYFILE}" --ssl-certfile "${HERMES_SSL_CERTFILE}")
fi
exec .venv/bin/uvicorn hub.main:app "${ARGS[@]}"
RUNEOF
chmod +x "$INSTALL_DIR/run.sh"

cp hermes-hub.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "Hermes Hub 已安装并启动"
echo "Access Token: $ACCESS_TOKEN"
echo "查看日志: journalctl -u $SERVICE_NAME -f"
