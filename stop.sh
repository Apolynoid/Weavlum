#!/usr/bin/env bash
# ============================================================
#  Weavlum 服务器停止 / 状态（Linux / WSL 通用）
#  用法：
#    ./stop.sh            # 停止服务器
#    ./stop.sh --status   # 只看状态（运行中退出码 0，未运行 1）
#  说明：按「Python + run.py + 本项目工作目录」三重校验定位进程，
#        只结束本项目的服务器，不会误杀其他 Python 程序或终端命令。
# ============================================================
set -e
cd "$(dirname "$0")"
PROJECT="$(pwd -P)"
PIDFILE="$PROJECT/data/server.pid"

# 判断某个 PID 是否为本项目的服务器进程。
# 必须同时满足：解释器是 python、参数里有 run.py、工作目录是项目根目录。
is_project_server() {
  local pid="$1" args=() base a found=0
  [ -n "$pid" ] && [ -d "/proc/$pid" ] || return 1
  mapfile -d '' args < "/proc/$pid/cmdline" 2>/dev/null || return 1
  [ "${#args[@]}" -ge 2 ] || return 1
  base="$(basename -- "${args[0]}")"
  case "$base" in
    python|python3|python3.*) ;;
    *) return 1 ;;
  esac
  for a in "${args[@]}"; do
    if [ "$(basename -- "$a")" = "run.py" ]; then
      found=1
      break
    fi
  done
  [ "$found" = "1" ] || return 1
  [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" = "$PROJECT" ] || return 1
  return 0
}

# 扫描所有进程，输出属于本项目的服务器 PID（覆盖没有 PID 文件的旧实例）。
project_pids() {
  local p
  for p in /proc/[0-9]*; do
    is_project_server "${p#/proc/}" && echo "${p#/proc/}"
  done
  return 0
}

# 汇总 PID 文件与进程扫描结果并去重。
collect_pids() {
  local pids="" pid
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    is_project_server "$pid" && pids="$pid"
  fi
  for pid in $(project_pids); do
    case " $pids " in
      *" $pid "*) ;;
      *) pids="$pids $pid" ;;
    esac
  done
  echo $pids
}

show_status() {
  local pids
  pids="$(collect_pids)"
  if [ -z "$pids" ]; then
    echo "服务器未在运行。"
    return 1
  fi
  echo "服务器运行中（PID:$pids）"
  return 0
}

case "$1" in
  --status|status)
    show_status
    exit $?
    ;;
esac

pids="$(collect_pids)"
if [ -z "$pids" ]; then
  rm -f "$PIDFILE"
  echo "服务器未在运行。"
  exit 0
fi

echo "正在停止服务器（PID:$pids）…"
for pid in $pids; do
  kill "$pid" 2>/dev/null || true
done

# 最多等 8 秒让 uvicorn 优雅退出，超时再强制结束。
for _ in $(seq 1 16); do
  alive=""
  for pid in $pids; do
    is_project_server "$pid" && alive="$alive $pid"
  done
  [ -z "$alive" ] && break
  sleep 0.5
done
for pid in $pids; do
  if is_project_server "$pid"; then
    kill -9 "$pid" 2>/dev/null || true
  fi
done
sleep 1
rm -f "$PIDFILE"

left="$(collect_pids)"
if [ -n "$left" ]; then
  echo "[警告] 仍有服务器进程：$left，请手动检查。"
  exit 1
fi
echo "服务器已停止。"
