"""
一次性脚本：把 data/srd/spells.json 的英文 desc / higher_level 翻译为中文
（写入 desc_zh / higher_zh）——车卡向导与角色卡的悬停说明使用。

- 幂等：已有 desc_zh 的条目自动跳过；每批翻译完立即落盘，可随时中断后重跑续译。
- 模型与密钥取自 data/settings.json（管理页配置的那套 LLM）。
- 用法：.venv/bin/python scripts/build_spell_descs.py

注意：若将来重跑 scripts/build_spells.py 重建法术数据，翻译字段会丢，需再跑本脚本。
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.state import load_settings  # noqa: E402（需先补好 sys.path 再导入）

SPELLS_FILE = ROOT / "data" / "srd" / "spells.json"
BATCH_SIZE = 8    # 每批翻译条数（太大易触发输出截断，太小费请求）

SYSTEM_PROMPT = (
    "你是 D&D 5e 中文规则翻译。把给定的英文法术条目翻译成简洁准确的中文（面向跑团玩家）。\n"
    "要求：\n"
    "1. 游戏术语用标准译法：advantage 优势 / disadvantage 劣势 / saving throw 豁免检定 / "
    "spell slot 法术位 / concentration 专注 / ritual 仪式 / cantrip 戏法 / bonus action 附赠动作 / "
    "action 动作 / reaction 反应 / creature 生物；feet 写\"尺\"。\n"
    "2. 骰子与数值原样保留（4d4、1d6+3、DC 15 等）。\n"
    "3. 只输出一个 JSON 对象，键为法术 id：{\"<id>\": {\"desc_zh\": \"...\", \"higher_zh\": \"...\"}}；"
    "没有 higher_level 的条目 higher_zh 输出空字符串。\n"
    "4. 不要输出任何解释文字或 markdown 代码块。"
)


def call_llm(settings, payload):
    """一次翻译请求；返回 {id: {desc_zh, higher_zh}}。异常由调用方重试。"""
    llm = settings.get("llm") or {}
    base = (llm.get("base_url") or "").rstrip("/")
    if not base:
        raise ValueError("data/settings.json 未配置 LLM base_url")
    headers = {"Content-Type": "application/json"}
    key = llm.get("api_key") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": llm.get("model") or "deepseek-chat",
        "temperature": 0,
        "max_tokens": 8000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    resp = httpx.post(base + "/chat/completions", json=body, headers=headers, timeout=180)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    # 容错：模型偶尔包一层 markdown 代码块
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return json.loads(content)


def main():
    settings = load_settings()
    data = json.loads(SPELLS_FILE.read_text(encoding="utf-8"))
    todo = [sid for sid, s in data.items() if not s.get("desc_zh")]
    print(f"待翻译 {len(todo)} / {len(data)} 条（已有 {len(data) - len(todo)} 条跳过）")
    done = 0
    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        payload = {
            sid: {
                "name": data[sid].get("name", ""),
                "desc": data[sid].get("desc", ""),
                "higher_level": data[sid].get("higher_level", ""),
            }
            for sid in batch
        }
        result = None
        for attempt in (1, 2):
            try:
                result = call_llm(settings, payload)
            except Exception as exc:    # 网络/解析失败：等待后重试一次
                print(f"  批次重试（{attempt}/2）：{exc}")
                time.sleep(2)
                continue
            filled = [
                sid for sid in batch
                if str((result.get(sid) or {}).get("desc_zh") or "").strip()
            ]
            if filled:
                break
            # 模型返回了合法 JSON 但结构跑偏（如多包了一层）：视为失败重试
            print(f"  批次返回不含目标条目（尝试 {attempt}/2），重试")
            result = None
            time.sleep(2)
        if not result:
            print(f"  批次失败，跳过（可重跑续译）：{batch}")
            continue
        for sid in batch:
            item = result.get(sid) or {}
            desc_zh = str(item.get("desc_zh") or "").strip()
            if desc_zh:
                data[sid]["desc_zh"] = desc_zh
            higher_zh = str(item.get("higher_zh") or "").strip()
            if higher_zh:
                data[sid]["higher_zh"] = higher_zh
        done += len(batch)
        SPELLS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  进度 {done}/{len(todo)}（已落盘）")
    remain = sum(1 for s in data.values() if not s.get("desc_zh"))
    print(f"完成：剩余未翻译 {remain} 条" + ("（重跑本脚本可续译）" if remain else ""))


if __name__ == "__main__":
    main()
