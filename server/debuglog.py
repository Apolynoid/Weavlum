"""
调试日志模块（debug 模式）。

用途：排查问题时的"黑匣子"。开启后把关键事件以 JSONL 落盘到 data/logs/，
每个事件一行 JSON：{"ts": 时间, "cat": 类别, "event": 事件名, ...其余字段}。

开启方式：
  - 启动参数：python run.py --debug（通过环境变量 WEAVLUM_DEBUG=1 传入）
  - 管理页运行时开关（本次进程有效）

安全与体积：
  - 敏感字段按 key 打码（api_key / host_token / authorization / token）；
    URL 里的 host_token=XXX 形态也会替换成 ***。
  - 单字段序列化超过 MAX_FIELD（20KB）时截断并标注（完整 prompt 等大字段不会撑爆日志）。

未开启时所有 log() 调用立即返回（零开销）。
"""

import json
import re
import threading
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
MAX_FIELD = 20 * 1024

# 按字段名打码的敏感 key
SENSITIVE_KEYS = ("api_key", "host_token", "authorization", "token")
# URL 查询串里的 host_token=xxx 形态
_TOKEN_URL_RE = re.compile(r"(host_token=)[A-Za-z0-9\-_]+")

_enabled = False
_file_path = None
_lock = threading.Lock()


def enable():
    """开启调试日志并创建本次进程的日志文件（重复调用无副作用）。"""
    global _enabled, _file_path
    if _enabled:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    _file_path = LOG_DIR / f"debug-{stamp}.jsonl"
    _file_path.touch(exist_ok=True)
    _enabled = True


def disable():
    global _enabled
    _enabled = False


def is_enabled():
    return _enabled


def current_file():
    """当前进程正在写的日志文件路径；未开启（或从未开启）时返回空串。"""
    return str(_file_path) if _file_path else ""


def _sanitize(value, key=None):
    """递归打码与截断：dict 按 key 打码，str 内查 token 形态并限制长度。"""
    if isinstance(value, dict):
        out = {}
        for child_key, child in value.items():
            if str(child_key).lower() in SENSITIVE_KEYS:
                out[child_key] = "***"
            else:
                out[child_key] = _sanitize(child, child_key)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        text = _TOKEN_URL_RE.sub(lambda m: m.group(1) + "***", value)
        if len(text) > MAX_FIELD:
            text = text[:MAX_FIELD] + f"…[截断 {len(text) - MAX_FIELD} 字符]"
        return text
    return value


def log(cat, event, **data):
    """写一条调试事件。cat 建议取值：ws / ai / engine / log / client。"""
    if not _enabled or _file_path is None:
        return
    payload = {"ts": round(time.time(), 3), "cat": cat, "event": event}
    payload.update(_sanitize(data))
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _lock:
        try:
            with open(_file_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass    # 日志失败不影响主流程


def list_files():
    """列出全部调试日志文件（新文件在前）。"""
    if not LOG_DIR.exists():
        return []
    items = []
    for path in sorted(LOG_DIR.glob("debug-*.jsonl"), reverse=True):
        stat = path.stat()
        items.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime})
    return items


def resolve_file(name):
    """把前端传来的文件名解析为安全路径（防路径穿越，只认 debug-*.jsonl）。"""
    safe = Path(str(name)).name
    if not safe.startswith("debug-") or not safe.endswith(".jsonl"):
        return None
    path = LOG_DIR / safe
    return path if path.exists() else None
