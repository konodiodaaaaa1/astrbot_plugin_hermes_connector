#!/usr/bin/env bash
# 手动更新 Hermes Hub，保留现有 Token / SSL / 容器配置
set -e

INSTALL_DIR="${HERMES_HUB_DIR:-/opt/hermes-hub}"
BACKUP_DIR="${HERMES_HUB_BACKUP_DIR:-/opt/hermes-hub-backups}"
RELEASE_URL="${HERMES_HUB_RELEASE:-https://github.com/ziyue67/astrbot_plugin_hermes_connector/releases/download/v1.3.0/hermes-hub.tar.gz}"

mkdir -p "$BACKUP_DIR"
timestamp=$(date +%Y%m%d-%H%M%S)
backup_path="$BACKUP_DIR/hermes-hub-$timestamp"

echo "[1/5] 备份当前 Hub -> $backup_path"
if [ -d "$INSTALL_DIR" ]; then
    cp -a "$INSTALL_DIR" "$backup_path"
fi

echo "[2/5] 下载新版 hermes-hub.tar.gz"
cd /tmp
rm -f hermes-hub.tar.gz
curl -sL -o hermes-hub.tar.gz "$RELEASE_URL"

echo "[3/5] 解压到临时目录"
rm -rf /tmp/hermes-hub-new
mkdir /tmp/hermes-hub-new
tar -xzf /tmp/hermes-hub.tar.gz -C /tmp/hermes-hub-new
chmod +x /tmp/hermes-hub-new/install.sh

echo "[4/5] 读取现有环境变量并执行安装"
. /etc/default/hermes-hub
cd /tmp/hermes-hub-new
HERMES_ACCESS_TOKEN="${HERMES_ACCESS_TOKEN}" \
HERMES_JWT_SECRET="${HERMES_JWT_SECRET}" \
HERMES_HOST="${HERMES_HOST:-127.0.0.1}" \
HERMES_PORT="${HERMES_PORT:-9800}" \
HERMES_BINARY="${HERMES_BINARY:-hermes}" \
HERMES_CONTAINER="${HERMES_CONTAINER:-}" \
HERMES_SSL_KEYFILE="${HERMES_SSL_KEYFILE:-}" \
HERMES_SSL_CERTFILE="${HERMES_SSL_CERTFILE:-}" \
bash ./install.sh

echo "[5/5] 验证服务"
sleep 2
systemctl is-active --quiet hermes-hub && echo "hermes-hub 运行正常" || echo "警告：hermes-hub 未运行，请查看日志"

echo ""
echo "============================"
echo "Hermes Hub 更新完成"
echo "备份路径: $backup_path"
echo "如需回滚: sudo rm -rf $INSTALL_DIR && sudo mkdir -p $INSTALL_DIR && sudo cp -a $backup_path/. $INSTALL_DIR/ && sudo systemctl restart hermes-hub"
echo "============================"
