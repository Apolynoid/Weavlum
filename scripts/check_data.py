"""
BFRD 数据校验（scripts/check_data.py）：
对 data/bfrd_staging/ 全量做字段完整性与交叉引用检查；退出码非 0 表示有错误。
用法：.venv/bin/python scripts/check_data.py
"""

import json
import re
import sys
from pathlib import Path

STAGING = Path(__file__).resolve().parents[1] / "data" / "bfrd_staging"

VALID_SOURCES = {"arcane", "divine", "primordial", "wyrd"}
VALID_LISTS = {"magic", "martial", "technical"}
SKILL_IDS = {
    "acrobatics", "animal_handling", "arcana", "athletics", "deception", "history", "insight",
    "intimidation", "investigation", "medicine", "nature", "perception", "performance",
    "persuasion", "religion", "sleight_of_hand", "stealth", "survival",
}

errors, warnings = [], []


def load(name, default=None):
    path = STAGING / f"{name}.json"
    if not path.exists():
        errors.append(f"缺文件：{name}.json")
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def check(condition, message):
    if not condition:
        errors.append(message)


def main():
    glossary = load("glossary", {})
    check(len(glossary.get("entries", [])) > 300, f"术语表条目过少：{len(glossary.get('entries', []))}")

    lineage = load("lineage", {})
    lineages, heritages = lineage.get("lineages", {}), lineage.get("heritages", {})
    check(len(lineages) == 6, f"血统应为 6 个，实为 {len(lineages)}")
    check(len(heritages) == 9, f"传承应为 9 个，实为 {len(heritages)}")

    gods = load("gods", {})
    check(len(gods) == 3, f"神系应为 3 个，实为 {len(gods)}")
    deity_total = sum(len(p.get("deities") or []) for p in gods.values())
    check(deity_total >= 35, f"神祇总数过少：{deity_total}")
    for key, pantheon in gods.items():
        check(bool(pantheon.get("name")) and bool(pantheon.get("desc")), f"神系 {key} 缺名字或导语")
        for deity in pantheon.get("deities") or []:
            check(bool(deity.get("name")) and bool(deity.get("name_en")), f"{key} 存在无名神祇")
            check(bool(deity.get("domains")), f"神祇 {deity.get('name')} 缺领域")
    for key, item in {**lineages, **heritages}.items():
        check(bool(item.get("name")) and bool(item.get("name_en")), f"{key} 缺名字")
        check(bool(item.get("traits_zh")), f"{key} 缺特性")

    talents = load("talents", {})
    check(len(talents) == 30, f"天赋应为 30 条，实为 {len(talents)}")
    for key, item in talents.items():
        check(item.get("list") in VALID_LISTS, f"天赋 {key} 列表非法：{item.get('list')}")
        check(bool(item.get("desc_zh") or item.get("benefits_zh")), f"天赋 {key} 缺内容")

    backgrounds = load("backgrounds", {})
    check(len(backgrounds) == 3, f"背景应为 3 个，实为 {len(backgrounds)}")
    for key, item in backgrounds.items():
        check(len(item.get("skill_choices") or []) >= 2, f"背景 {key} 技能选项不足")
        check(all(s in SKILL_IDS for s in item.get("skill_choices") or []), f"背景 {key} 技能 id 非法")
        check(len(item.get("talent_choices") or []) == 3, f"背景 {key} 天赋三选一缺失")
        for tid in item.get("talent_choices") or []:
            check(tid in talents, f"背景 {key} 引用了不存在的天赋 {tid}")

    chargen = load("chargen", {})
    pb = chargen.get("pointbuy_costs") or {}
    check(chargen.get("pointbuy_total") == 32, "点买总点数应为 32")
    check(len(pb) >= 11 and all(f"{s}" in pb or s in pb for s in range(8, 19)), "点买表不完整")
    check(chargen.get("standard_array") == [16, 14, 14, 13, 10, 8], "标准数组不符")

    conditions = load("conditions", {})
    check(len(conditions) >= 15, f"状态过少：{len(conditions)}")

    spells = load("spells", {})
    check(len(spells) >= 300, f"法术过少：{len(spells)}")
    ritual_count = 0
    for key, item in spells.items():
        check(item.get("level") is not None, f"法术 {key} 缺环阶")
        check(bool(item.get("school")), f"法术 {key} 缺学派")
        check(all(s in VALID_SOURCES for s in item.get("sources") or []), f"法术 {key} 来源非法")
        check(bool(item.get("sources")), f"法术 {key} 来源为空")
        if item.get("ritual"):
            ritual_count += 1
    check(ritual_count >= 60, f"仪式数量异常：{ritual_count}")

    items = load("items", {})
    check(len(items) >= 350, f"物品过少：{len(items)}")
    magic = sum(1 for v in items.values() if v.get("category") == "Magic")
    check(magic >= 200, f"魔法物品过少：{magic}")
    for key, item in items.items():
        check(bool(item.get("name")), f"物品 {key} 缺名字")
        if item.get("category") == "Weapon":
            check(bool(item.get("damage")), f"武器 {key} 缺伤害")

    monsters = load("monsters", {})
    check(len(monsters) >= 350, f"怪物过少：{len(monsters)}")
    no_attack = []
    for key, item in monsters.items():
        check(item.get("ac") and item.get("hp"), f"怪物 {key} 缺 AC/HP")
        check(item.get("xp"), f"怪物 {key} 缺 XP")
        check(bool(item.get("traits_zh")), f"怪物 {key} 缺 traits")
        if not item.get("attack"):
            no_attack.append(key)
    known_no_attack = {"shrieker", "frog", "sea-horse", "vampire-mist"}
    unexpected = [k for k in no_attack if k not in known_no_attack]
    if unexpected:
        warnings.append(f"无主攻击（需人工确认）：{unexpected}")

    classes = load("classes", {})
    check(len(classes) == 13, f"职业应为 13 个，实为 {len(classes)}")
    for key, item in classes.items():
        check(item.get("hit_die"), f"职业 {key} 缺生命骰")
        check(item.get("saves"), f"职业 {key} 缺豁免")
        check(bool(item.get("features")), f"职业 {key} 缺特性")
        check(len(item.get("table") or {}) == 20, f"职业 {key} 进度表应 20 级")
        progression = item.get("progression") or {}
        prog_rows = progression.get("rows") or {}
        check(len(prog_rows) == 20, f"职业 {key} 进阶表应 20 级（progression）")
        prog_cols = {c.get("key") for c in progression.get("columns") or []}
        check({"level", "pb", "features"} <= prog_cols, f"职业 {key} 进阶表缺通用列")
        spot = {
            "barbarian": ("rage_damage", "1"),
            "rogue": ("sneak_attack", "1"),
            "sorcerer": ("sorcery_points", "1"),
        }
        if key in spot:
            ck, lv = spot[key]
            check(bool(prog_rows.get(lv, {}).get(ck)), f"职业 {key} 进阶表 {lv} 级缺 {ck}")
        # 特性 ↔ 进度表交叉校验：解析等级须等于该名称在表中最先出现的等级（占位/复合条目跳过）
        table_min = {}
        for lv, row in (item.get("table") or {}).items():
            for fname in row.get("features") or []:
                base = re.sub(r"[（(].*$", "", fname).strip().split("：")[0].strip()
                if base and "子职业" not in base:
                    table_min[base] = min(table_min.get(base, 999), int(lv))
        for slug, feat in (item.get("features") or {}).items():
            base = re.sub(r"[（(].*$", "", feat.get("name") or "").strip()
            if base in table_min:
                check(
                    int(feat.get("level") or 0) == table_min[base],
                    f"{key}/{slug} 特性等级 {feat.get('level')} ≠ 进度表 {table_min[base]}",
                )
        # 子职业特性归属标记（标记应含子职业名；「领域」可省略）
        for sid, sub in (item.get("subclasses") or {}).items():
            sname = sub.get("name") or ""
            candidates = {sname, sname.replace("领域", "").strip()} - {""}
            check(len(str(sub.get("desc") or "").strip()) >= 10, f"{key}/{sid} 子职业缺导语")
            for fslug, feat in (sub.get("features") or {}).items():
                marker = str(feat.get("marker_zh") or "")
                check(any(c in marker for c in candidates), f"{key}/{sid}/{fslug} 标记异常：{marker}")
        # 提升（Improvement）档位完整性
        imp_levels = [lv for lv, row in (item.get("table") or {}).items() if any("提升" in f for f in row.get("features") or [])]
        check(len(imp_levels) >= 5, f"职业 {key} 提升档位不足：{imp_levels}")
        if item.get("spell_ability"):
            check(bool(item.get("sources")), f"施法职业 {key} 缺法术来源")
            check(all(s in VALID_SOURCES for s in item.get("sources") or []), f"职业 {key} 来源非法")
            for lvl in ("1", "5", "20"):
                check(lvl in (item.get("slots") or {}), f"职业 {key} 缺 {lvl} 级法术位")
        for sub_key, sub in (item.get("subclasses") or {}).items():
            check(bool(sub.get("features")), f"子职业 {key}/{sub_key} 缺特性")

    print(f"校验完成：错误 {len(errors)}，警告 {len(warnings)}")
    for message in errors:
        print("  ✗", message)
    for message in warnings:
        print("  ⚠", message)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
