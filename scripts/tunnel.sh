#!/usr/bin/env bash
# ============================================================
# Weavlum 独立公网隧道（Cloudflare Quick Tunnel）
# 用法：
#   ./scripts/tunnel.sh            # 前台启动，默认转发 8000
#   ./scripts/tunnel.sh 8010       # 指定端口
#   ./scripts/tunnel.sh --status   # 查看隧道是否在运行
#   ./scripts/tunnel.sh --stop     # 主动结束后台隧道
# 前置：先启动 Weavlum 服务器（./start.sh），保持运行。
# 说明：无需账号/域名/路由器配置；启动后会打印
#       https://<随机子域>.trycloudflare.com 公网网址，发给队友即可。
#       首次运行会下载 cloudflared（约 40MB，GitHub 不通时自动走镜像）。
#       网址每次重启都会变化；前台窗口按 Ctrl+C 即断开。
# ============================================================
set -e
cd "$(dirname "$0")/.."
mkdir -p data/bin
BIN="data/bin/cloudflared"
PIDFILE="data/bin/tunnel.pid"

# 读取 PID 文件并确认该进程仍是本项目隧道；成功时输出 PID。
running_pid() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'cloudflared' || return 1
  echo "$pid"
}

# 兜底：按二进制路径+参数精确匹配（不会匹配本脚本自身）。
fallback_pids() {
  pgrep -f "$BIN tunnel --url" 2>/dev/null || true
}

stop_tunnel() {
  local pid
  pid="$(running_pid 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    echo "正在停止隧道（PID $pid）…"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.3
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  if [ -n "$(fallback_pids)" ]; then
    echo "清理残留 cloudflared 进程…"
    pkill -f "$BIN tunnel --url" 2>/dev/null || true
    sleep 1
  fi
  if [ -n "$(fallback_pids)" ]; then
    echo "[警告] 仍有残留进程，请手动检查：pgrep -af cloudflared"
    return 1
  fi
  echo "隧道已停止。"
}

show_status() {
  local pid
  pid="$(running_pid 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    echo "隧道运行中（PID $pid）"
    return 0
  fi
  if [ -n "$(fallback_pids)" ]; then
    echo "检测到未登记的隧道进程：$(fallback_pids | tr '\n' ' ')"
    return 0
  fi
  echo "隧道未运行。"
  return 1
}

case "$1" in
  --stop|stop)
    stop_tunnel
    exit $?
    ;;
  --status|status)
    show_status
    exit $?
    ;;
esac

if [ -n "$(running_pid 2>/dev/null || true)" ] || [ -n "$(fallback_pids)" ]; then
  echo "已有隧道在运行；如需重启，先执行：./scripts/tunnel.sh --stop"
  exit 1
fi

PORT="${1:-8000}"

if [ ! -x "$BIN" ] || ! "$BIN" --version >/dev/null 2>&1; then
  case "$(uname -m)" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "[错误] 不支持的架构：$(uname -m)"; exit 1 ;;
  esac
  ASSET="cloudflared-linux-$ARCH"
  fetch() {
    # $1 = URL, $2 = 说明
    echo "$2（最长等待 120 秒）…"
    rm -f data/bin/.cloudflared.tmp
    if curl -fL -# --connect-timeout 10 --max-time 120 -o data/bin/.cloudflared.tmp "$1" \
       && [ "$(stat -c%s data/bin/.cloudflared.tmp 2>/dev/null || echo 0)" -gt 20000000 ]; then
      chmod +x data/bin/.cloudflared.tmp
      if data/bin/.cloudflared.tmp --version >/dev/null 2>&1; then
        mv data/bin/.cloudflared.tmp "$BIN"
        return 0
      fi
    fi
    rm -f data/bin/.cloudflared.tmp
    return 1
  }
  GH="https://github.com/cloudflare/cloudflared/releases/latest/download/$ASSET"
  if ! fetch "$GH" "[1/3] GitHub 直连下载" \
     && ! fetch "https://gh-proxy.com/$GH" "[2/3] GitHub 不通，改用镜像 gh-proxy.com" \
     && ! fetch "https://ghproxy.net/$GH" "[3/3] 改用镜像 ghproxy.net"; then
    echo "[错误] cloudflared 下载失败（网络受限）。"
    echo "       可手动下载 $ASSET 放到 data/bin/cloudflared 并 chmod +x。"
    exit 1
  fi
  echo "[ok] cloudflared 就绪。"
fi

echo "正在建立 Cloudflare 临时隧道 → http://127.0.0.1:$PORT"
echo "稍后出现的 https://<随机子域>.trycloudflare.com 就是公网网址（Ctrl+C 结束）"
echo "停止隧道：另开终端执行 ./scripts/tunnel.sh --stop"
echo
echo $$ > "$PIDFILE"
exec "$BIN" tunnel --url "http://127.0.0.1:$PORT" --protocol http2 --no-autoupdate
