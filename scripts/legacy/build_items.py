"""
构建物品数据（开发期一次性工具，运行后把结果提交到 data/srd/items.json）。

流程：
  1. 从 jsDelivr 拉取 5e-bits/5e-database 的 SRD 5.1 装备与魔法物品（英文，CC-BY-4.0）
  2. 结构化：
     - 装备 237 条全收（武器含伤害/射程/属性；护甲含 AC 公式；杂物/工具/坐骑含说明）
     - 魔法物品只收 普通/罕见/稀有 三档（战役等级 1-6 可达；含变体叶子条目，丢弃带 variants 的父条目）
     - 价格统一折算为铜币；魔法物品按稀有度估价（普通 100 金 / 罕见 400 金 / 稀有 4000 金）
  3. 翻译：两趟 LLM 请求——A 趟翻全部名称（批量），B 趟翻有描述条目的描述（小批量，防截断）
  4. 名称补丁：与本平台护甲选项目名对齐（链甲/皮甲等），保证建卡装备能对上库
  5. 写出 data/srd/items.json（幂等：已有 name_zh/desc_zh 的跳过，可中断续跑）

用法：.venv/bin/python scripts/build_items.py
注意：需要网络（走环境代理）与 data/settings.json 中配置的 LLM。
"""

import json
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://cdn.jsdelivr.net/gh/5e-bits/5e-database@main/src/2014/en/"
OUT_FILE = ROOT / "data" / "srd" / "items.json"
SETTINGS_FILE = ROOT / "data" / "settings.json"

NAME_BATCH = 30     # 名称翻译批大小（输出短，可大）
DESC_BATCH = 6      # 描述翻译批大小（输出长，防截断）
MAX_TOKENS = 8000

CATEGORY_ZH = {
    "Weapon": "武器",
    "Armor": "护甲",
    "Adventuring Gear": "冒险用品",
    "Tools": "工具",
    "Mounts and Vehicles": "坐骑与载具",
    "Potion": "药水",
    "Ring": "戒指",
    "Rod": "权杖",
    "Scroll": "卷轴",
    "Staff": "法杖",
    "Wand": "魔杖",
    "Wondrous Items": "奇物",
    "Wondrous item": "奇物",
    "Trade Goods": "贸易品",
}
RARITY_ZH = {"Common": "普通", "Uncommon": "罕见", "Rare": "稀有"}
RARITY_PRICE_CP = {"Common": 10000, "Uncommon": 40000, "Rare": 400000}  # 按稀有度估价（铜币）
UNIT_CP = {"cp": 1, "sp": 10, "gp": 100, "pp": 1000}
DAMAGE_ZH = {
    "bludgeoning": "钝击",
    "piercing": "穿刺",
    "slashing": "斩击",
}
PROPERTY_ZH = {
    "Ammunition": "弹药",
    "Finesse": "灵巧",
    "Heavy": "重型",
    "Light": "轻型",
    "Loading": "装填",
    "Reach": "触及",
    "Special": "特殊",
    "Thrown": "投掷",
    "Two-Handed": "双手",
    "Versatile": "可换持（双手）",
}
ARMOR_CATEGORY_ZH = {"Light": "轻甲", "Medium": "中甲", "Heavy": "重甲", "Shield": "盾牌"}
WEAPON_CATEGORY_ZH = {"Simple": "简易", "Martial": "军用"}

# 与本平台护甲选项目名对齐（characters.py ARMOR_OPTIONS），保证建卡装备字符串能匹配上库
NAME_PATCH = {
    "leather": "皮甲",
    "studded-leather-armor": "镶钉皮甲",
    "chain-shirt": "链甲衫",
    "scale-mail": "鳞甲",
    "chain-mail": "链甲",
    "plate-armor": "板甲",
    "shield": "盾牌",
}

NAME_SYSTEM = (
    "你是 D&D 5e 中文译者。把给定的英文物品名翻译成中文通用译名（面向跑团玩家，如 Potion of Healing→治疗药水、"
    "Rope→绳索、Backpack→背包、Longsword→长剑、Chain Mail→链甲）。\n"
    "只输出一个 JSON 对象：{\"<id>\": \"中文名\"}；不要输出解释或 markdown 代码块。"
)

DESC_SYSTEM = (
    "你是 D&D 5e 中文规则翻译。把给定的英文物品说明翻译成简洁准确的中文（面向跑团玩家）。\n"
    "要求：\n"
    "1. 术语用标准译法：advantage 优势 / saving throw 豁免检定 / attunement 同调 / "
    "hit points 生命值 / DC 保持 DC / feet 写\"尺\"。\n"
    "2. 骰子与数值原样保留（2d4+2、DC 15 等）。\n"
    "3. 只输出一个 JSON 对象：{\"<id>\": \"中文说明\"}；不要输出解释或 markdown 代码块。"
)


def load_llm():
    """读取管理页配置的 LLM（与 server/state.py 的 settings.json 一致）。"""
    data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    llm = data.get("llm") or {}
    base = (llm.get("base_url") or "").rstrip("/")
    if not base:
        raise ValueError("data/settings.json 未配置 LLM base_url")
    return {
        "url": base + "/chat/completions",
        "headers": {"Content-Type": "application/json", "Authorization": f"Bearer {llm.get('api_key', '')}"},
        "model": llm.get("model") or "deepseek-chat",
    }


def llm_json(llm, system, payload):
    """一次 LLM 请求并解析 JSON；异常由调用方重试。"""
    body = {
        "model": llm["model"],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    resp = httpx.post(llm["url"], json=body, headers=llm["headers"], timeout=180)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return json.loads(content)


def run_batches(llm, system, todo, payload_of, apply_fn, batch_size, label, on_batch=None):
    """通用批处理：失败重试一次；批次内一条都没返回视为失败重试；每批回调落盘。"""
    done = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        payload = {sid: payload_of(sid) for sid in batch}
        result = None
        for attempt in (1, 2):
            try:
                result = llm_json(llm, system, payload)
            except Exception as exc:
                print(f"  [{label}] 批次重试（{attempt}/2）：{exc}")
                time.sleep(2)
                continue
            hit = [sid for sid in batch if str(result.get(sid) or "").strip()]
            if hit:
                break
            print(f"  [{label}] 返回不含目标条目（尝试 {attempt}/2），重试")
            result = None
            time.sleep(2)
        if not result:
            print(f"  [{label}] 批次失败跳过（可重跑续译）：{batch}")
            continue
        apply_fn(result)
        done += len(batch)
        if on_batch:
            on_batch()
        print(f"  [{label}] 进度 {done}/{len(todo)}")


def fetch_json(client, name):
    resp = client.get(BASE + name)
    resp.raise_for_status()
    return resp.json()


def cost_to_cp(cost):
    if not isinstance(cost, dict):
        return None
    try:
        qty = float(cost.get("quantity") or 0)
    except (TypeError, ValueError):
        return None
    unit = str(cost.get("unit", "")).lower()
    if unit not in UNIT_CP:
        return None
    return int(round(qty * UNIT_CP[unit]))


def build_structure(client):
    """抓取并结构化（不含翻译）；返回 {id: item}。"""
    equipment = fetch_json(client, "5e-SRD-Equipment.json")
    magic = fetch_json(client, "5e-SRD-Magic-Items.json")
    items = {}

    for entry in equipment:
        idx = entry.get("index")
        if not idx:
            continue
        cat = (entry.get("equipment_category") or {}).get("name", "")
        item = {
            "name_en": entry.get("name", ""),
            "category": cat,
            "category_zh": CATEGORY_ZH.get(cat, cat),
            "cost_cp": cost_to_cp(entry.get("cost")),
            "price_note": "",
            "weight": entry.get("weight"),
            "rarity_zh": "",
            "desc_en": " ".join(entry.get("desc") or []).strip(),
        }
        # 武器：伤害/射程/属性/类别
        if cat == "Weapon":
            dmg = entry.get("damage") or {}
            item["weapon_category_zh"] = WEAPON_CATEGORY_ZH.get(entry.get("weapon_category", ""), "")
            item["damage"] = dmg.get("damage_dice", "")
            item["damage_type_zh"] = DAMAGE_ZH.get((dmg.get("damage_type") or {}).get("index", ""), "")
            two = entry.get("two_handed_damage") or {}
            if two:
                item["versatile_damage"] = two.get("damage_dice", "")
            rng = entry.get("range") or {}
            if entry.get("weapon_range") == "Ranged":
                item["range_text"] = f"远程（{rng.get('normal', '')}/{rng.get('long', '')} 尺）"
            elif rng.get("normal"):
                item["range_text"] = f"投掷（{rng.get('normal', '')}/{rng.get('long', '')} 尺）"
            else:
                item["range_text"] = "近战"
            props = [
                PROPERTY_ZH.get((p or {}).get("name", ""), (p or {}).get("name", ""))
                for p in entry.get("properties") or []
            ]
            item["properties_zh"] = [p for p in props if p]
        # 护甲：AC 公式/力量门槛/隐匿劣势/类别
        if cat == "Armor":
            ac = entry.get("armor_class") or {}
            item["armor_category_zh"] = ARMOR_CATEGORY_ZH.get(entry.get("armor_category", ""), "")
            if entry.get("armor_category") == "Shield":
                item["ac_text"] = f"AC +{ac.get('base', 2)}"
            elif ac.get("dex_bonus"):
                maxb = ac.get("max_bonus")
                item["ac_text"] = f"AC {ac.get('base', 10)} + 敏捷加值" + (f"（最高 +{maxb}）" if maxb else "")
            else:
                item["ac_text"] = f"AC {ac.get('base', 10)}"
            if entry.get("str_minimum"):
                item["str_minimum"] = entry["str_minimum"]
            if entry.get("stealth_disadvantage"):
                item["stealth_disadvantage"] = True
        items[idx] = item

    for entry in magic:
        idx = entry.get("index")
        if not idx or entry.get("variants"):
            continue    # 带 variants 的是父条目，保留叶子
        rarity = (entry.get("rarity") or {}).get("name", "")
        if rarity not in RARITY_ZH:
            continue    # 只收 普通/罕见/稀有
        cat = (entry.get("equipment_category") or {}).get("name", "")
        item = {
            "name_en": entry.get("name", ""),
            "category": cat,
            "category_zh": CATEGORY_ZH.get(cat, cat),
            "cost_cp": RARITY_PRICE_CP.get(rarity),
            "price_note": "（按稀有度估价）",
            "weight": None,
            "rarity_zh": RARITY_ZH[rarity],
            "desc_en": " ".join(entry.get("desc") or []).strip(),
        }
        items[idx] = item

    return items


def main():
    llm = load_llm()
    with httpx.Client(timeout=60) as client:
        fresh = build_structure(client)

    if OUT_FILE.exists():
        old = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        # 合并：保留已有翻译结果（幂等续跑），新结构字段以本次为准
        for idx, item in fresh.items():
            prev = old.get(idx) or {}
            for key in ("name_zh", "desc_zh"):
                if prev.get(key):
                    item[key] = prev[key]
        data = fresh
    else:
        data = fresh
    print(f"条目总数 {len(data)}（含描述 {sum(1 for s in data.values() if s['desc_en'])} 条）")

    # A 趟：名称翻译
    todo = [idx for idx, s in data.items() if not s.get("name_zh")]
    print(f"名称待译 {len(todo)} 条")
    run_batches(
        llm, NAME_SYSTEM, todo,
        payload_of=lambda idx: data[idx]["name_en"],
        apply_fn=lambda res: _apply_names(data, res),
        batch_size=NAME_BATCH, label="名称",
        on_batch=lambda: _save(data),
    )
    _save(data)

    # B 趟：描述翻译（仅有英文描述的条目）
    todo = [idx for idx, s in data.items() if s["desc_en"] and not s.get("desc_zh")]
    print(f"描述待译 {len(todo)} 条")
    run_batches(
        llm, DESC_SYSTEM, todo,
        payload_of=lambda idx: data[idx]["desc_en"],
        apply_fn=lambda res: _apply_descs(data, res),
        batch_size=DESC_BATCH, label="描述",
        on_batch=lambda: _save(data),
    )
    _save(data)

    missing_name = sum(1 for s in data.values() if not s.get("name_zh"))
    missing_desc = sum(1 for s in data.values() if s["desc_en"] and not s.get("desc_zh"))
    print(f"完成：未译名 {missing_name}，未译描述 {missing_desc}（可重跑续译）")


def _apply_names(data, result):
    for idx, name in result.items():
        if idx in data and str(name).strip():
            data[idx]["name_zh"] = str(name).strip()


def _apply_descs(data, result):
    for idx, desc in result.items():
        if idx in data and str(desc).strip():
            data[idx]["desc_zh"] = str(desc).strip()


def _save(data):
    """写盘前套名称补丁（护甲统一用本平台译名）。"""
    for idx, name in NAME_PATCH.items():
        if idx in data:
            data[idx]["name_zh"] = name
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
