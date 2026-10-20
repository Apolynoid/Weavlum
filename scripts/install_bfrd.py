"""
把 BFRD 校验通过的 staging 数据安装为平台正式数据（data/srd/）。
执行前请确认 data/_srd51_backup/ 已存在（v0.4.1 时已备份）。

用法：.venv/bin/python scripts/install_bfrd.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data" / "bfrd_staging"
DATA = ROOT / "data" / "srd"
BACKUP = ROOT / "data" / "_srd51_backup"

# 准备施法者的动态上限规则（能力调整值 + 每等级系数 × 等级，向下取整后至少 1）
PREPARED_RULES = {
    "cleric": {"ability": "wis", "per_level": 1},
    "druid": {"ability": "wis", "per_level": 1},
    "wizard": {"ability": "int", "per_level": 1},
    "paladin": {"ability": "cha", "per_level": 0.5},
    "ranger": {"ability": "wis", "per_level": 0.5},
}


def load(name):
    return json.loads((STAGING / f"{name}.json").read_text(encoding="utf-8"))


def save(name, data):
    (DATA / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  → data/srd/{name}.json（{len(data)} 条）")


def main():
    assert BACKUP.exists(), "缺少 data/_srd51_backup/ 备份，请先备份再安装"

    print("[安装] 血统 → races.json / heritages.json")
    lineage = load("lineage")
    races = {}
    for key, item in lineage["lineages"].items():
        traits = item.get("traits_zh") or []
        desc_lines = [f"{t['name']}：{t['desc']}" for t in traits]
        adaptations = item.get("adaptations_zh") or []
        for adapt in adaptations:
            desc_lines.append(f"【{adapt['name']}】{adapt['desc']}")
        races[key] = {
            "name": item["name"], "name_en": item["name_en"],
            "bonuses": {}, "speed": item["speed"],
            "traits": "、".join(t["name"] for t in traits),
            "traits_desc": "\n".join(desc_lines),
            "adaptation_names": [a["name"] for a in adaptations],
            "adaptations": [
                {"name": a.get("name", ""), "name_en": a.get("name_en", ""), "desc": a.get("desc", "")}
                for a in adaptations
            ],
        }
    save("races", races)
    heritages = {}
    for key, item in lineage["heritages"].items():
        traits = item.get("traits_zh") or []
        heritages[key] = {
            "name": item["name"], "name_en": item["name_en"],
            "traits": "、".join(t["name"] for t in traits),
            "traits_desc": "\n".join(f"{t['name']}：{t['desc']}" for t in traits),
        }
    save("heritages", heritages)

    print("[安装] 天赋 / 背景")
    save("talents", load("talents"))
    backgrounds = {}
    for key, item in load("backgrounds").items():
        backgrounds[key] = {
            "name": item["name"], "name_en": item["name_en"],
            "skill_choices": item["skill_choices"], "skill_count": item["skill_count"],
            "additional_zh": item.get("additional_zh", ""),
            "equipment_zh": item.get("equipment_zh", ""),
            "talent_choices": item["talent_choices"],
            "desc": (item.get("additional_zh", "") + "；装备：" + item.get("equipment_zh", ""))[:300],
        }
    save("backgrounds", backgrounds)

    print("[安装] 职业")
    classes = load("classes")
    for key, item in classes.items():
        if item.get("spell_ability"):
            item["prepared_rule"] = PREPARED_RULES.get(key)
        else:
            item["prepared_rule"] = None
        # 兼容建卡向导的默认护甲字段（v0.5.2 向导重做前保持可用）
        item.setdefault("default_armor", "none")
        item.setdefault("default_shield", False)
    save("classes", classes)

    print("[安装] 法术（+ 兼容职业表）")
    spells = load("spells")
    for spell in spells.values():
        legacy_classes = [
            cid for cid, klass in classes.items()
            if klass.get("sources") and set(spell.get("sources") or []) & set(klass["sources"])
        ]
        spell["classes"] = legacy_classes
    save("spells", spells)

    print("[安装] 规则表 spell_rules.json")
    caster_classes = [cid for cid, k in classes.items() if k.get("sources")]
    slots = {cid: classes[cid]["slots"] for cid in caster_classes}
    counts = {}
    for cid in caster_classes:
        klass = classes[cid]
        counts[cid] = {
            "cantrips": [klass.get("cantrips_known", {}).get(str(l), 0) for l in range(1, 21)],
            "spells": ([klass["spells_known"].get(str(l), 0) for l in range(1, 21)] if klass.get("spells_known") else None),
            "prepared_rule": klass.get("prepared_rule"),
        }
    old_recommended = {}
    old_path = BACKUP / "spell_rules.json"
    if old_path.exists():
        old_recommended = json.loads(old_path.read_text(encoding="utf-8")).get("recommended") or {}
    # 推荐包：只保留"存在且来源匹配该职业"的条目；不足时用该职业来源的低环法术补齐
    recommended = {}
    for cid in caster_classes:
        sources = set(classes[cid].get("sources") or [])
        picks = [sid for sid in (old_recommended.get(cid) or []) if sid in spells and set(spells[sid].get("sources") or []) & sources]
        if len(picks) < 6:
            pool = sorted(
                (sid for sid, s in spells.items() if set(s.get("sources") or []) & sources and s.get("level", 0) <= 1 and not s.get("ritual")),
                key=lambda sid: (spells[sid]["level"], sid),
            )
            for sid in pool:
                if sid not in picks:
                    picks.append(sid)
                if len(picks) >= 8:
                    break
        recommended[cid] = picks
    save("spell_rules", {
        "caster_classes": caster_classes,
        "slots": slots,
        "counts": counts,
        "recommended": recommended,
    })

    print("[安装] 物品 / 怪物 / 创建表 / 状态 / 术语表")
    save("items", load("items"))
    save("monsters", load("monsters"))
    save("chargen", load("chargen"))
    save("conditions", load("conditions"))
    save("glossary", load("glossary"))

    print("[安装] 神祇与神系 → gods.json")
    save("gods", load("gods"))

    print("[安装] 技能表（术语对齐：求生/游说/杂技）")
    skills = json.loads((BACKUP / "skills.json").read_text(encoding="utf-8"))
    ren = {"survival": "求生", "persuasion": "游说", "acrobatics": "杂技"}
    for sid, label in ren.items():
        if sid in skills:
            skills[sid]["name"] = label
    save("skills", skills)

    print("安装完成。")


if __name__ == "__main__":
    main()
