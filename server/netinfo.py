"""
网络信息辅助：检测本机对局域网可见的 IP。

欢迎页"分享给玩家"与 run.py 启动提示共用这一份逻辑——之前只在启动器里检测、
页面拿不到，导致主机点"复制网址"复制出的是 localhost（发给别人不可用）。

策略：
  - 先向公网地址开个 UDP"连接"拿到默认出口 IP（不会真的发包）
  - 再补充主机名解析出的地址
  - 过滤回环/链路本地/docker 网桥等无效地址，优先 192.168.x
"""

import socket


def candidate_ips():
    """返回对局域网可能可达的 IP 列表（可能为空——比如断网时）。"""
    ips = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        primary = probe.getsockname()[0]
        probe.close()
        ips.append(primary)
    except OSError:
        pass
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = item[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    skip = ("127.", "169.254.", "172.17.", "172.18.", "10.255.", "0.")
    good = [ip for ip in ips if not ip.startswith(skip)]
    good.sort(key=lambda ip: (not ip.startswith("192.168."), not ip.startswith("10."), ip))
    return good

