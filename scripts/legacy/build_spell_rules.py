"""
构建法术规则表（data/srd/spell_rules.json 的 slots / counts 两节，1-20 级）。

数据源：5e-bits/5e-database 的 5e-SRD-Levels.json（含每职业每级的
  prof_bonus / cantrips_known / spells_known / spell_slots_level_1..9）。
策略：
  - 有 spells_known 的职业（吟游/术士/契术师）直接用源数据
  - 法师按"法术书"线性增长：6 + 2×(等级-1)
  - 牧师/德鲁伊（准备施法）按 等级 + 3 的静态上限近似
  - 圣武士/游侠（半施法准备）按 ceil(等级/2) + 1 近似
  - recommended（推荐包）沿用现有文件内容，不重生成
用法：.venv/bin/python scripts/build_spell_rules.py
"""

import json
import math
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://cdn.jsdelivr.net/gh/5e-bits/5e-database@main/src/2014/en/5e-SRD-Levels.json"
OUT_FILE = ROOT / "data" / "srd" / "spell_rules.json"
CASTER_CLASSES = ["bard", "cleric", "druid", "paladin", "ranger", "sorcerer", "warlock", "wizard"]


def slot_row(casting):
    """法术位字段 → 到最高环为止的数组（内部 0 保留；无环为空数组）。"""
    values = []
    for ring in range(1, 10):
        values.append(int(casting.get(f"spell_slots_level_{ring}") or 0))
    while values and values[-1] == 0:
        values.pop()
    return values


def spells_cap(class_index, level, casting):
    """已知/可准备法术数上限（近似策略见模块 docstring）。"""
    known = casting.get("spells_known")
    if known:
        return int(known)
    if class_index == "wizard":
        return 6 + 2 * (level - 1)
    if class_index in ("cleric", "druid"):
        return level + 3
    if class_index in ("paladin", "ranger"):
        if level < 2:
            return 0
        return math.ceil(level / 2) + 1
    return 0


def main():
    response = httpx.get(SOURCE_URL, timeout=120, follow_redirects=True)
    response.raise_for_status()
    rows = response.json()
    by_class = {}
    for row in rows:
        cls = (row.get("class") or {}).get("index")
        if cls not in CASTER_CLASSES:
            continue
        if row.get("subclass"):     # 跳过子职业行，只取主职业 1-20
            continue
        level = int(row.get("level") or 0)
        if not 1 <= level <= 20:
            continue
        by_class.setdefault(cls, {})[level] = row

    old = {}
    if OUT_FILE.exists():
        old = json.loads(OUT_FILE.read_text(encoding="utf-8"))

    slots = {}
    counts = {}
    for cls in CASTER_CLASSES:
        levels = by_class.get(cls) or {}
        slots[cls] = {}
        cantrips = []
        spells = []
        for level in range(1, 21):
            row = levels.get(level) or {}
            casting = row.get("spellcasting") or {}
            slots[cls][str(level)] = slot_row(casting)
            cantrips.append(int(casting.get("cantrips_known") or 0))
            spells.append(spells_cap(cls, level, casting))
        counts[cls] = {"cantrips": cantrips, "spells": spells}

    payload = {
        "caster_classes": CASTER_CLASSES,
        "slots": slots,
        "counts": counts,
        "recommended": old.get("recommended") or {},
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    wiz9 = slots["wizard"]["9"]
    war11 = slots["warlock"]["11"]
    pal5 = slots["paladin"]["5"]
    print(f"已写出 {OUT_FILE.relative_to(ROOT)}")
    print(f"抽查：法师9级位 {wiz9} ｜ 契术师11级位 {war11} ｜ 圣武士5级位 {pal5}")
    print(f"抽查：法师20级法术书上限 {counts['wizard']['spells'][19]} ｜ 牧师20级 {counts['cleric']['spells'][19]}")


if __name__ == "__main__":
    main()
