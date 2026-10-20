"""
构建法术数据（开发期一次性工具，运行后把结果提交到 data/srd/spells.json）。

流程：
  1. 从 jsDelivr 拉取 5e-bits/5e-database 的 SRD 5.1 法术数据（英文，CC-BY-4.0）
  2. 过滤 0-9 环（v0.4.0 起支持 1-20 级）
  3. 结构化：学派/施法时间/距离/成分/时长（含折算轮数）/伤害与逐环提升/豁免/职业表
  4. 已有翻译（中文名/中文描述）按 id 保留；新法术调 LLM 翻译名称（失败回退英文）
  5. 写出 data/srd/spells.json（描述翻译随后跑 scripts/build_spell_descs.py）

用法：.venv/bin/python scripts/build_spells.py
注意：需要网络（走环境代理）与 data/settings.json 中配置的 LLM。
"""

import json
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SOURCE_URL = "https://cdn.jsdelivr.net/gh/5e-bits/5e-database@main/src/2014/en/5e-SRD-Spells.json"
OUT_FILE = ROOT / "data" / "srd" / "spells.json"
SETTINGS_FILE = ROOT / "data" / "settings.json"

SCHOOL_ZH = {
    "abjuration": "防护",
    "conjuration": "咒法",
    "divination": "预言",
    "enchantment": "附魔",
    "evocation": "塑能",
    "illusion": "幻术",
    "necromancy": "死灵",
    "transmutation": "变化",
}
CASTING_TIME_ZH = {
    "1 action": "1 动作",
    "1 bonus action": "1 附赠动作",
    "1 reaction": "1 反应",
    "1 minute": "1 分钟",
    "10 minutes": "10 分钟",
    "1 hour": "1 小时",
    "12 hours": "12 小时",
    "24 hours": "24 小时",
    "special": "特殊",
}
COMPONENT_ZH = {"V": "言语", "S": "姿势", "M": "材料"}
SAVE_ZH = {"str": "力量", "dex": "敏捷", "con": "体质", "int": "智力", "wis": "感知", "cha": "魅力"}
DAMAGE_ZH = {
    "acid": "强酸",
    "bludgeoning": "钝击",
    "cold": "冷冻",
    "fire": "火焰",
    "force": "力场",
    "lightning": "闪电",
    "necrotic": "死灵",
    "piercing": "穿刺",
    "poison": "毒素",
    "psychic": "心灵",
    "radiant": "光耀",
    "slashing": "斩击",
    "thunder": "雷鸣",
}
UNIT_ROUNDS = {"round": 1, "minute": 10, "hour": 600, "day": 14400}

# 数据源怪值修正：多发类法术在源数据里按"单发"记骰，与我们的"一次掷总量"模型不符。
PATCHES = {
    # 魔能爆：源数据所有等级都是 1d10（实际是升级增加光束数）；
    # 引擎一次掷一次伤害，折算成等价总量（5/11/17 级各 +1d10）。
    "eldritch-blast": {
        "damage": {
            "type": "力场",
            "by_char_level": {"1": "1d10", "5": "2d10", "11": "3d10", "17": "4d10"},
            "attack": "远程法术攻击",
            "note": "规则的实体是「多道光束」（5/11/17 级各 +1 道），此处按总量折算为一次伤害掷骰",
        }
    },
    # 灼热射线：源数据按"每道射线 2d6"记；补一条说明供 AI 逐道结算。
    "scorching-ray": {
        "damage_note": "创造 3 道射线，每道单独进行法术攻击、单道造成 2d6 火焰伤害；每提升 1 环增加 1 道射线",
    },
}


def apply_patches(result):
    """把已知的数据源怪值修正应用到转换结果上。"""
    for key, patch in PATCHES.items():
        if key in result:
            result[key].update(patch)
    return result


def duration_rounds(text):
    """把英文时长折算成战斗轮数（立即/特殊/直到解除 → 0）。"""
    lowered = (text or "").lower()
    if "instantaneous" in lowered or "special" in lowered or "until dispelled" in lowered:
        return 0
    lowered = lowered.replace("up to ", "")
    match = re.search(r"(\d+)\s*(round|minute|hour|day)", lowered)
    if not match:
        return 0
    return int(match.group(1)) * UNIT_ROUNDS[match.group(2)]


def duration_zh(text):
    """时长转中文（保留"专注"前缀）。"""
    text = (text or "").strip()
    concentration = text.lower().startswith("concentration")
    base = re.sub(r"(?i)^concentration,?\s*(up to\s*)?", "", text)
    base = re.sub(r"(?i)^up to\s*", "", base)
    zh_map = {
        "instantaneous": "立即",
        "special": "特殊",
        "until dispelled": "直到被解除",
    }
    lowered = base.lower()
    if lowered in zh_map:
        base_zh = zh_map[lowered]
    else:
        match = re.match(r"(\d+)\s*(round|minute|hour|day)s?", lowered)
        if match:
            unit = {"round": "轮", "minute": "分钟", "hour": "小时", "day": "天"}[match.group(2)]
            base_zh = f"{match.group(1)} {unit}"
        else:
            base_zh = base
    return ("专注，" if concentration else "") + base_zh


def range_zh(text):
    """距离简单本地化（尺/英里/自身/触碰等）。"""
    text = str(text or "")
    text = re.sub(r"(\d+)\s*feet?", r"\1 尺", text, flags=re.I)
    text = re.sub(r"(\d+)\s*miles?", r"\1 英里", text, flags=re.I)
    text = re.sub(r"\bself\b", "自身", text, flags=re.I)
    text = re.sub(r"\btouch\b", "触碰", text, flags=re.I)
    text = re.sub(r"\bsight\b", "视线", text, flags=re.I)
    text = re.sub(r"\bunlimited\b", "无限", text, flags=re.I)
    return text


def damage_block(spell):
    """抽取伤害/豁免/攻击/治疗等结构化信息（引擎据此掷骰，AI 不用解析文本）。"""
    out = {}
    damage_list = spell.get("damage") or []
    if damage_list:
        first = damage_list[0]
        dtype = first.get("damage_type") or {}
        out["type"] = DAMAGE_ZH.get(dtype.get("index"), dtype.get("name", ""))
        if first.get("damage_at_slot_level"):
            out["by_slot"] = first["damage_at_slot_level"]
        if first.get("damage_at_character_level"):
            out["by_char_level"] = first["damage_at_character_level"]
    dc = spell.get("dc") or {}
    if dc.get("dc_type"):
        out["save"] = SAVE_ZH.get(dc["dc_type"]["index"], dc["dc_type"]["name"])
        out["save_success"] = dc.get("dc_success")
    desc = " ".join(spell.get("desc") or [])
    if re.search(r"ranged spell attack", desc, re.I):
        out["attack"] = "远程法术攻击"
    elif re.search(r"melee spell attack", desc, re.I):
        out["attack"] = "近战法术攻击"
    heal = re.search(r"regains? (?:a number of )?hit points equal to ([^.;]+)", desc, re.I)
    if heal:
        out["heal"] = heal.group(1).strip()[:80]
    return out or None


def transform(spell):
    """单条法术：英文原始数据 → 我们的结构化条目。"""
    school = (spell.get("school") or {}).get("index", "")
    classes = [
        c.get("index")
        for c in (spell.get("classes") or [])
        if c.get("index") in ("bard", "cleric", "druid", "paladin", "ranger", "sorcerer", "warlock", "wizard")
    ]
    duration = spell.get("duration", "")
    return {
        "name": spell.get("name", ""),          # 先填英文，稍后由翻译结果覆盖
        "name_en": spell.get("name", ""),
        "level": int(spell.get("level", 0)),
        "school": SCHOOL_ZH.get(school, school),
        "casting_time": CASTING_TIME_ZH.get(spell.get("casting_time", ""), spell.get("casting_time", "")),
        "range": range_zh(spell.get("range", "")),
        "components": [COMPONENT_ZH.get(c, c) for c in (spell.get("components") or [])],
        "material": spell.get("material") or "",
        "ritual": bool(spell.get("ritual")),
        "duration": duration_zh(duration),
        "concentration": bool(spell.get("concentration")),
        "duration_rounds": duration_rounds(duration),
        "damage": damage_block(spell),
        "desc": " ".join(spell.get("desc") or [])[:900],
        "higher_level": " ".join(spell.get("higher_level") or [])[:400],
        "classes": classes,
    }


def translate_names(names):
    """调用 DeepSeek 一次性翻译法术名（官方/通用译名优先）；失败返回空映射。"""
    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))["llm"]
    base = (settings.get("base_url") or "").rstrip("/")
    key = settings.get("api_key") or ""
    if not base or not key:
        print("！未配置 LLM，跳过翻译（将使用英文名）")
        return {}
    prompt = (
        "你是 D&D 5e 中文规则翻译专家。请给出以下法术名最通用的中文译名（官方译名优先，采用社区通行译法），"
        "输出一个 JSON 对象，键为英文名、值为中文名，不要输出任何其他文字。\n"
        + json.dumps(names, ensure_ascii=False)
    )
    response = httpx.post(
        base + "/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        json={"model": settings.get("model") or "deepseek-chat", "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    response.raise_for_status()
    text = (response.json()["choices"][0]["message"].get("content") or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        mapping = json.loads(text)
    except json.JSONDecodeError:
        print("！翻译结果不是有效 JSON，回退英文名")
        return {}
    return {str(k): str(v) for k, v in mapping.items() if isinstance(mapping, dict)}


def main():
    print("拉取 SRD 法术数据 …")
    response = httpx.get(SOURCE_URL, timeout=120, follow_redirects=True)
    response.raise_for_status()
    raw = response.json()
    print(f"原始 {len(raw)} 条；过滤 0-9 环 …")
    selected = [s for s in raw if int(s.get("level", 99)) <= 9]
    print(f"保留 {len(selected)} 条")

    result = {}
    for spell in selected:
        result[spell["index"]] = transform(spell)
    apply_patches(result)

    # 保留已有翻译（中文名/中文描述），避免重建时丢失翻译成本
    kept = 0
    if OUT_FILE.exists():
        old = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        for idx, entry in result.items():
            prev = old.get(idx) or {}
            for key in ("name", "desc_zh", "higher_zh"):
                if prev.get(key):
                    entry[key] = prev[key]
                    if key == "name":
                        kept += 1
    print(f"沿用已有中文名 {kept} 条")

    todo_names = sorted({e["name_en"] for e in result.values() if e["name"] == e["name_en"]})
    print(f"翻译新法术名 {len(todo_names)} 条 …")
    mapping = translate_names(todo_names)
    translated = 0
    for entry in result.values():
        if entry["name"] == entry["name_en"]:
            zh = mapping.get(entry["name_en"], "").strip()
            if zh:
                entry["name"] = zh
                translated += 1
    print(f"新名翻译覆盖 {translated}/{len(todo_names)} 条（其余回退英文）")

    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {OUT_FILE.relative_to(ROOT)}（{OUT_FILE.stat().st_size // 1024} KB，共 {len(result)} 条）")


if __name__ == "__main__":
    main()
