"""
构建怪物数据（全量 SRD 5.1，334 条）→ data/srd/monsters.json。

流程（幂等，可中断续跑，每批落盘）：
  1. 从 jsDelivr 拉取 5e-bits/5e-database 的 5e-SRD-Monsters.json
  2. 结构化：AC/HP/步行速度/先攻（敏捷修正）、体型/类型/阵营/感知/抗免/技能豁免/语言
     （本地映射表翻译）；主攻击取首个带命中加值的动作（暂存原始加值，译完后对位命名）
  3. LLM 翻译：名称（批量）+ 特性/动作/反应/传奇动作的名称与描述（按字符预算分批）
  4. 合成 traits_zh（图鉴悬停与 AI 查询用）；旧手写 desc 按英文名保留
  5. 写出 data/srd/monsters.json

用法：.venv/bin/python scripts/build_monsters.py
"""

import json
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://cdn.jsdelivr.net/gh/5e-bits/5e-database@main/src/2014/en/5e-SRD-Monsters.json"
OUT_FILE = ROOT / "data" / "srd" / "monsters.json"
SETTINGS_FILE = ROOT / "data" / "settings.json"

NAME_BATCH = 30
TEXT_BATCH_MAX = 4            # 文本翻译每批最多怪物数
TEXT_INPUT_BUDGET = 3600      # 每批输入字符预算（输出约等长，防截断）
MAX_TOKENS = 8000

SIZE_ZH = {"Tiny": "微型", "Small": "小型", "Medium": "中型", "Large": "大型", "Huge": "巨型", "Gargantuan": "超巨型"}
TYPE_ZH = {
    "aberration": "异怪", "beast": "野兽", "celestial": "天界生物", "construct": "构装体",
    "dragon": "龙类", "elemental": "元素", "fey": "妖精", "fiend": "邪魔", "giant": "巨人",
    "humanoid": "类人生物", "monstrosity": "怪诞生物", "ooze": "泥怪", "plant": "植物",
    "undead": "亡灵", "swarm of Tiny beasts": "微型野兽虫群",
}
SUBTYPE_ZH = {"goblinoid": "地精类", "shapechanger": "变形生物", "devil": "魔鬼", "demon": "恶魔", "human": "人类"}
ALIGN_ZH = {
    "lawful good": "守序善良", "neutral good": "中立善良", "chaotic good": "混乱善良",
    "lawful neutral": "守序中立", "neutral": "中立", "chaotic neutral": "混乱中立",
    "lawful evil": "守序邪恶", "neutral evil": "中立邪恶", "chaotic evil": "混乱邪恶",
    "unaligned": "无阵营", "any alignment": "任意阵营", "any good alignment": "任意善良阵营",
    "any non-lawful alignment": "任意非守序阵营", "any chaotic alignment": "任意混乱阵营",
    "any evil alignment": "任意邪恶阵营", "neutral good (50%) or neutral evil (50%)": "中立善良或中立邪恶",
}
DAMAGE_ZH = {
    "acid": "强酸", "bludgeoning": "钝击", "cold": "冷冻", "fire": "火焰", "force": "力场",
    "lightning": "闪电", "necrotic": "死灵", "piercing": "穿刺", "poison": "毒素",
    "psychic": "心灵", "radiant": "光耀", "slashing": "斩击", "thunder": "雷鸣",
}
COND_ZH = {
    "blinded": "目盲", "charmed": "魅惑", "deafened": "耳聋", "exhaustion": "力竭",
    "frightened": "恐慌", "grappled": "擒抱", "incapacitated": "失能", "invisible": "隐形",
    "paralyzed": "麻痹", "petrified": "石化", "poisoned": "中毒", "prone": "倒地",
    "restrained": "束缚", "stunned": "震慑", "unconscious": "昏迷",
}
SKILL_ZH = {
    "acrobatics": "体操", "animal-handling": "驯兽", "arcana": "奥秘", "athletics": "运动",
    "deception": "欺瞒", "history": "历史", "insight": "洞悉", "intimidation": "威吓",
    "investigation": "调查", "medicine": "医药", "nature": "自然", "perception": "察觉",
    "performance": "表演", "persuasion": "说服", "religion": "宗教", "sleight-of-hand": "巧手",
    "stealth": "隐匿", "survival": "生存",
}
ABILITY_ZH = {"strength": "力量", "dexterity": "敏捷", "constitution": "体质", "intelligence": "智力", "wisdom": "感知", "charisma": "魅力"}
LANG_ZH = {
    "common": "通用语", "goblin": "地精语", "draconic": "龙语", "dwarvish": "矮人语",
    "elvish": "精灵语", "giant": "巨人语", "gnoll": "豺狼人语", "gnome": "侏儒语",
    "halfling": "半身人语", "orc": "兽人语", "abyssal": "深渊语", "celestial": "天界语",
    "deep speech": "深潜语", "infernal": "炼狱语", "primordial": "原初语", "sylvan": "木族语",
    "undercommon": "地底通用语", "aquan": "水族语", "auran": "风族语", "ignan": "火族语",
    "terran": "土族语", "telepathy": "心灵感应",
}
SENSE_ZH = {"darkvision": "黑暗视觉", "blindsight": "盲视", "tremorsense": "震颤感知", "truesight": "真实视觉"}
SPEED_ZH = {"fly": "飞行", "swim": "游泳", "climb": "攀爬", "burrow": "掘进", "hover": "悬停"}

NAME_SYSTEM = (
    "你是 D&D 5e 中文译者。把给定的英文怪物名翻译成中文通用译名（社区通行/官方译名优先，"
    "如 Goblin→地精、Adult Red Dragon→成年红龙、Beholder→眼魔）。"
    "只输出一个 JSON 对象：{\"<id>\": \"中文名\"}；不要输出解释或 markdown 代码块。"
)
TEXT_SYSTEM = (
    "你是 D&D 5e 中文规则翻译。把给定怪物的特性/动作/反应/传奇动作逐条翻译成准确的中文（面向跑团 DM）。\n"
    "要求：\n"
    "1. 术语标准译法：advantage 优势 / saving throw 豁免检定 / DC 保持 DC / hit points 生命值 / "
    "reach 触及范围内 / ft. 写\"尺\" / multiattack 多重攻击 / recharge 充能。\n"
    "2. 骰子与数值原样保留（2d6+4、DC 15 等）；\"Melee Weapon Attack: +5 to hit … Hit: 12 (2d6+5) slashing damage\""
    " 译为\"近战武器攻击：+5 命中，1d6+2 斩击伤害\"式的简洁表达。\n"
    "3. 输入是 {\"<id>\": {\"abilities\": [{name, desc}], \"actions\": [...], \"reactions\": [...], \"legendary\": [...]}}，"
    "输出相同结构、名称与描述全部为中文。\n"
    "4. 不要输出任何解释或 markdown 代码块。"
)


def load_llm():
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
    body = {
        "model": llm["model"],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    resp = httpx.post(llm["url"], json=body, headers=llm["headers"], timeout=240)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return json.loads(content)


def run_batches(llm, system, todo, payload_of, apply_fn, label, budget_of=None, on_batch=None, max_count=TEXT_BATCH_MAX):
    """按字符预算分批（预算内最多 max_count 条）；失败重试一次；每批回调落盘。"""
    done = 0
    index = 0
    while index < len(todo):
        batch = []
        cost = 0
        while index + len(batch) < len(todo) and len(batch) < max_count:
            nxt = todo[index + len(batch)]
            nxt_cost = budget_of(nxt) if budget_of else 0
            if batch and cost + nxt_cost > TEXT_INPUT_BUDGET:
                break
            cost += nxt_cost
            batch.append(nxt)
        index += len(batch)
        payload = {sid: payload_of(sid) for sid in batch}
        result = None
        for attempt in (1, 2):
            try:
                result = llm_json(llm, system, payload)
            except Exception as exc:
                print(f"  [{label}] 批次重试（{attempt}/2）：{exc}")
                time.sleep(2)
                continue
            if [sid for sid in batch if result.get(sid)]:
                break
            print(f"  [{label}] 返回不含目标条目（尝试 {attempt}/2），重试")
            result = None
            time.sleep(2)
        if not result:
            print(f"  [{label}] 批次失败跳过（可重跑续译）：{batch[:6]}…")
            continue
        apply_fn(result)
        done += len(batch)
        if on_batch:
            on_batch()
        print(f"  [{label}] 进度 {done}/{len(todo)}")


def cr_label(cr):
    mapping = {0: "0", 0.125: "1/8", 0.25: "1/4", 0.5: "1/2"}
    value = float(cr)
    if value in mapping:
        return mapping[value]
    return str(int(value)) if value == int(value) else str(value)


def parse_speed(speed):
    """速度字典 → (步行速度 int, 中文速度文本)。"""
    walk = 30
    parts = []
    for key, text in (speed or {}).items():
        match = re.search(r"(\d+)", str(text))
        feet = int(match.group(1)) if match else 0
        if key == "walk":
            walk = feet or 30
            parts.insert(0, f"步行 {walk} 尺")
        elif key == "hover":
            parts.append("悬停")
        elif feet:
            parts.append(f"{SPEED_ZH.get(key, key)} {feet} 尺")
    return walk, " / ".join(parts) or "步行 30 尺"


def senses_lines(m):
    out = []
    for key, zh in SENSE_ZH.items():
        text = (m.get("senses") or {}).get(key)
        if text:
            match = re.search(r"(\d+)", str(text))
            if match:
                out.append(f"{zh} {match.group(1)} 尺")
    passive = (m.get("senses") or {}).get("passive_perception")
    if passive:
        out.append(f"被动察觉 {passive}")
    return out


def defenses_lines(m):
    def keys(values):
        out = []
        for x in values or []:
            key = x.get("index") if isinstance(x, dict) else str(x)
            out.append(key)
        return out

    out = []
    for field, name in (
        ("damage_resistances", "伤害抗性"),
        ("damage_immunities", "伤害免疫"),
        ("damage_vulnerabilities", "伤害易伤"),
    ):
        values = [DAMAGE_ZH.get(k, k) for k in keys(m.get(field))]
        if values:
            out.append(f"{name}：" + "、".join(values))
    cond = [COND_ZH.get(k, k) for k in keys(m.get("condition_immunities"))]
    if cond:
        out.append("状态免疫：" + "、".join(cond))
    return out


def prof_lines(m):
    out = []
    for entry in m.get("proficiencies") or []:
        name = str((entry.get("proficiency") or {}).get("name", ""))
        value = entry.get("value")
        if name.startswith("Skill: "):
            key = name[len("Skill: "):].lower().replace(" ", "-")
            out.append(f"技能 {SKILL_ZH.get(key, key)} {value:+d}")
        elif name.startswith("Saving Throw: "):
            key = name[len("Saving Throw: "):].lower()
            out.append(f"豁免 {ABILITY_ZH.get(key, key)} {value:+d}")
    return out


def languages_text(m):
    parts = []
    for raw in m.get("languages") or []:
        low = str(raw).lower().strip()
        if low in ("—", "-", ""):
            continue
        if low.startswith("understands ") and "but can't speak" in low:
            target = low[len("understands "):].split(" but")[0].strip()
            parts.append(f"听得懂{LANG_ZH.get(target, target)}但不会说")
        else:
            parts.append(LANG_ZH.get(low, raw))
    return "、".join(parts)


def attack_raw(m):
    """首个带命中加值的动作 → 暂存（译完后用中文动作名对位）。"""
    for i, action in enumerate(m.get("actions") or []):
        if action.get("attack_bonus") is None:
            continue
        damage = action.get("damage") or []
        if not damage:
            continue
        first = damage[0]
        dtype = DAMAGE_ZH.get((first.get("damage_type") or {}).get("index"), "")
        return {"i": i, "to_hit": int(action["attack_bonus"]), "damage": f"{first.get('damage_dice', '')} {dtype}".strip()}
    return None


def build_structure(raw, old):
    """结构化（不含 LLM）；保留旧的名称/特性翻译与手写 desc。"""
    data = {}
    for m in raw:
        idx = m.get("index")
        if not idx:
            continue
        walk, speed_text = parse_speed(m.get("speed"))
        cr = float(m.get("challenge_rating") or 0)
        type_zh = TYPE_ZH.get(m.get("type", ""), m.get("type", ""))
        subtype = m.get("subtype") or ""
        if subtype:
            type_zh += f"（{SUBTYPE_ZH.get(subtype, subtype)}）"
        entry = {
            "name": m.get("name", ""),
            "name_en": m.get("name", ""),
            "cr": cr,
            "cr_label": cr_label(cr),
            "xp": int(m.get("xp") or 0),
            "ac": int((m.get("armor_class") or [{}])[0].get("value") or 10),
            "hp": int(m.get("hit_points") or 1),
            "speed": walk,
            "speed_text": speed_text,
            "init_bonus": (int(m.get("dexterity", 10)) - 10) // 2,
            "size_zh": SIZE_ZH.get(m.get("size", ""), m.get("size", "")),
            "type_zh": type_zh,
            "alignment_zh": ALIGN_ZH.get(str(m.get("alignment", "")).lower(), m.get("alignment", "")),
            "senses_zh": senses_lines(m),
            "defenses_zh": defenses_lines(m),
            "prof_zh": prof_lines(m),
            "languages_zh": languages_text(m),
            "_attack_raw": attack_raw(m),
            "_actions_en": [
                {"name": a.get("name", ""), "desc": a.get("desc", "")} for a in (m.get("actions") or [])
            ],
            "_abilities_en": [
                {"name": a.get("name", ""), "desc": a.get("desc", "")} for a in (m.get("special_abilities") or [])
            ],
            "_reactions_en": [
                {"name": a.get("name", ""), "desc": a.get("desc", "")} for a in (m.get("reactions") or [])
            ],
            "_legendary_en": [
                {"name": a.get("name", ""), "desc": a.get("desc", "")} for a in (m.get("legendary_actions") or [])
            ],
        }
        prev = old.get(idx) or {}
        for key in ("name", "actions_zh", "abilities_zh", "reactions_zh", "legendary_zh", "desc"):
            if prev.get(key):
                entry[key] = prev[key]
        data[idx] = entry
    return data


def compose(data, strip=False):
    """
    合成 traits_zh 与主攻击（幂等，随时可调用）。
    注意：只在中途保存时不剥离 _*_en 中间字段（否则后续翻译批拿不到源文本）；
    strip=True 仅在全部翻译完成后调用。
    """
    for entry in data.values():
        traits = ["速度：" + entry.get("speed_text", "")]
        traits += entry.get("senses_zh") or []
        traits += entry.get("defenses_zh") or []
        traits += entry.get("prof_zh") or []
        if entry.get("languages_zh"):
            traits.append("语言：" + entry["languages_zh"])
        for item in entry.get("abilities_zh") or []:
            traits.append(f"{item.get('name', '')}：{item.get('desc', '')}")
        for item in entry.get("actions_zh") or []:
            traits.append(f"{item.get('name', '')}：{item.get('desc', '')}")
        for item in entry.get("reactions_zh") or []:
            traits.append(f"反应——{item.get('name', '')}：{item.get('desc', '')}")
        for item in entry.get("legendary_zh") or []:
            traits.append(f"传奇动作——{item.get('name', '')}：{item.get('desc', '')}")
        entry["traits_zh"] = [t for t in traits if t and not t.endswith("：")]
        # 主攻击：暂存加值 + 中文动作名（缺中文时回退英文名）
        raw = entry.get("_attack_raw")
        attack = {}
        if raw:
            zh_list = entry.get("actions_zh") or []
            name = ""
            if 0 <= raw["i"] < len(zh_list):
                name = str((zh_list[raw["i"]] or {}).get("name", ""))
            if not name:
                en_list = entry.get("_actions_en") or []
                if 0 <= raw["i"] < len(en_list):
                    name = str((en_list[raw["i"]] or {}).get("name", ""))
            attack = {"name": name, "to_hit": raw["to_hit"], "damage": raw["damage"]}
        entry["attack"] = attack
    if strip:
        for entry in data.values():
            for key in ("_attack_raw", "_actions_en", "_abilities_en", "_reactions_en", "_legendary_en"):
                entry.pop(key, None)


def main():
    llm = load_llm()
    print("拉取 SRD 怪物数据 …")
    resp = httpx.get(SOURCE_URL, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    raw = resp.json()
    old = {}
    if OUT_FILE.exists():
        old = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        # 旧文件是成品格式（无中间字段）：其 name/desc 仍可沿用
    data = build_structure(raw, old)
    print(f"条目 {len(data)}（沿用旧翻译 {sum(1 for e in data.values() if e.get('actions_zh'))} 条）")

    def save(strip=False):
        compose(data, strip=strip)
        OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # A 趟：名称
    todo = [idx for idx, e in data.items() if e["name"] == e["name_en"]]
    print(f"名称待译 {len(todo)} 条")

    def apply_names(res):
        for idx, value in res.items():
            if idx in data and str(value).strip():
                data[idx]["name"] = str(value).strip()

    run_batches(llm, NAME_SYSTEM, todo, lambda idx: data[idx]["name_en"], apply_names, "名称", on_batch=save, max_count=NAME_BATCH)

    # B 趟：特性/动作/反应/传奇动作文本
    def payload_of(idx):
        e = data[idx]
        return {
            "abilities": e.get("_abilities_en") or [],
            "actions": e.get("_actions_en") or [],
            "reactions": e.get("_reactions_en") or [],
            "legendary": e.get("_legendary_en") or [],
        }

    def needs_text(idx):
        """任一英文清单存在而对应中文缺失，即需要（重）翻译。"""
        e = data[idx]
        for src, dst in (
            ("_abilities_en", "abilities_zh"),
            ("_actions_en", "actions_zh"),
            ("_reactions_en", "reactions_zh"),
            ("_legendary_en", "legendary_zh"),
        ):
            if e.get(src) and not e.get(dst):
                return True
        return False

    todo = [idx for idx in data if needs_text(idx)]
    print(f"特性/动作待译 {len(todo)} 条")

    def apply_text(res):
        for idx, item in res.items():
            if idx not in data or not isinstance(item, dict):
                continue
            for src, dst in (("abilities", "abilities_zh"), ("actions", "actions_zh"), ("reactions", "reactions_zh"), ("legendary", "legendary_zh")):
                value = item.get(src)
                if isinstance(value, list):
                    data[idx][dst] = value

    budget_of = lambda idx: len(json.dumps(payload_of(idx), ensure_ascii=False))
    run_batches(llm, TEXT_SYSTEM, todo, payload_of, apply_text, "特性", budget_of=budget_of, on_batch=save)

    miss = sum(1 for e in data.values() if not e.get("actions_zh") and (e.get("_actions_en") or e.get("_abilities_en")))
    save(strip=True)
    print(f"完成：{len(data)} 条，未译文本 {miss} 条（可重跑续译）")


if __name__ == "__main__":
    main()
