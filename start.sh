#!/usr/bin/env bash
# ============================================================
#  Weavlum 一键启动（Linux / WSL 通用）
#  用法：
#    ./start.sh            # 启动服务器（终端里会打印玩家网址与管理网址）
#    ./start.sh --open     # 启动并自动打开浏览器（管理页）
#    ./start.sh --debug    # 附带调试日志
#    ./start.sh --stop     # 停止服务器（等同 ./stop.sh）
#    ./start.sh --status   # 查看服务器状态
#  首次运行会自动创建虚拟环境并安装依赖（约 1-2 分钟）。
#  防止重复启动：已有实例时直接拒绝，避免悄悄换到 8001 端口。
# ============================================================
set -e
cd "$(dirname "$0")"
PROJECT="$(pwd -P)"
PIDFILE="$PROJECT/data/server.pid"
export PYTHONUNBUFFERED=1   # 输出不缓冲：启动横幅/日志即使被重定向也能实时看到

# --stop / --status 直接转交给 stop.sh。
for arg in "$@"; do
  case "$arg" in
    --stop|stop) exec "$PROJECT/stop.sh" ;;
    --status) exec "$PROJECT/stop.sh" --status ;;
  esac
done

# 判断某个 PID 是否为本项目的服务器进程（解释器 + run.py + 工作目录三重校验）。
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

server_pids() {
  local p
  for p in /proc/[0-9]*; do
    is_project_server "${p#/proc/}" && echo "${p#/proc/}"
  done
  return 0
}

existing="$(server_pids | tr '\n' ' ' | sed 's/ *$//')"
if [ -n "$existing" ]; then
  echo "服务器已在运行（PID: $existing）。"
  echo "如需重启，先执行：./stop.sh"
  exit 1
fi
rm -f "$PIDFILE"

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

if [ ! -x ".venv/bin/python" ]; then
  echo "首次运行：创建虚拟环境并安装依赖（约 1-2 分钟）…"
  "$PY" -m venv .venv
  # pip 源默认走清华镜像（下载快）；想换官方源：
  #   PIP_INDEX_URL=https://pypi.org/simple ./start.sh
  PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    .venv/bin/pip install --progress-bar off --timeout 60 -r requirements.txt
fi

# PID 文件记录的是本 shell 的 PID；下面 exec 后 PID 不变，正好对应服务器进程。
mkdir -p data
echo $$ > "$PIDFILE"
exec .venv/bin/python run.py "$@"
