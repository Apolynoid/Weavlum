"""
Weavlum 启动器（唯一入口：python run.py）

职责：
  1. 检查虚拟环境是否存在（不存在给提示）
  2. 选一个可用端口（8000 起，带 SO_REUSEADDR 避免 TIME_WAIT 误判）
  3. 生成/读取固定的主机管理 token（存 data/host_token.txt，重启不变）
  4. 检测局域网 IP 并打印玩家可用网址
  5. 用 uvicorn 启动 server.main:app（绑定 0.0.0.0，局域网可访问）

本文件不含业务逻辑；所有业务代码都在 server/ 目录下。
"""

import os
import secrets
import socket
import sys
from pathlib import Path

from server.netinfo import candidate_ips

ROOT = Path(__file__).resolve().parent


def pick_port():
    """从 8000 起找第一个可绑定端口。SO_REUSEADDR 让刚重启过的端口也能立即复用。"""
    for port in range(8000, 8011):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise SystemExit("8000-8010 端口均被占用")


def open_browser(url):
    """
    尽力用系统默认浏览器打开网址（三平台兼容）：
    - Windows 原生 Python：os.startfile
    - WSL：借 powershell.exe 打开 Windows 侧浏览器
    - Linux 桌面：webbrowser / xdg-open
    失败静默——服务器照常运行，终端里始终打印可用网址。
    """
    import threading
    import time as _time

    def worker():
        _time.sleep(1.5)    # 等 uvicorn 真正 listen
        try:
            if sys.platform == "win32":
                os.startfile(url)    # type: ignore[attr-defined]
            elif Path("/mnt/c/Windows/System32/cmd.exe").exists():
                import subprocess
                subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"]
                )
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def main():
    # --debug：开启调试日志（WS/AI/引擎事件写入 data/logs/debug-*.jsonl）
    debug = "--debug" in sys.argv[1:]
    if debug:
        os.environ["WEAVLUM_DEBUG"] = "1"
    open_flag = "--open" in sys.argv[1:]    # 启动后自动打开服务器管理页
    # 依赖装在项目内的 .venv 里，防止误用系统 Python 跑不起来
    if not (ROOT / ".venv" / "bin" / "python").exists():
        print("提示：尚未创建虚拟环境，请先运行：")
        print("  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
        return
    port = pick_port()
    # 主机管理 token：首次生成后固定保存，重启不变（管理页 URL 可以收藏）
    token_file = ROOT / "data" / "host_token.txt"
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip() or secrets.token_hex(3).upper()
    else:
        token = secrets.token_hex(3).upper()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token, encoding="utf-8")
    # 通过环境变量传给 server.main（它在导入时读取）
    os.environ["WEAVLUM_HOST_TOKEN"] = token
    os.chdir(ROOT)
    ips = candidate_ips()
    print("=" * 58)
    print("  Weavlum · 黑旗 SRD 局域网跑团")
    print("=" * 58)
    print("服务器管理页（本机浏览器打开，独立于玩家界面）:")
    manage_url = f"http://localhost:{port}/manage?host_token={token}"
    print(f"    {manage_url}")
    print("分享给玩家（局域网，选中复制）:")
    if ips:
        for ip in ips:
            print(f"    http://{ip}:{port}/")
    else:
        print("    （未检测到局域网 IP，本机可用 localhost 测试）")
    print("本机测试：同一台电脑多开浏览器窗口，分别用上面的网址进入")
    print(f"防火墙拦截时：Windows 管理员 PowerShell 运行 scripts\\windows_firewall.ps1 -Port {port}")
    if debug:
        print("调试模式：开启（日志写入 data/logs/，管理页可开关与下载）")
    print("=" * 58)
    if open_flag:
        open_browser(manage_url)
    # 延迟到这里才导入 uvicorn，保证上面的提示能先打印出来
    import uvicorn
    uvicorn.run("server.main:app", host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
