"""
构建 BFRD（Black Flag SRD v0.2）中文译稿数据 → data/bfrd_staging/*.json。

纯解析、零 LLM：中文译稿已含全部内容（英文原名以括注保留），本脚本把 markdown
转为平台 schema 的 JSON（展示文本剥离「（English）」括注；id 取英文括注 slugify）。
分节运行：.venv/bin/python scripts/build_bfrd.py --only glossary|lineage|talents|backgrounds|chargen|conditions|spells|items|monsters|classes|all
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bfrd import (  # noqa: E402
    ABILITY_ABBR_ID, ABILITY_ZH_ID, DAMAGE_ZH, ROOT, SKILL_ZH_ID, SOURCE_EN_ID, SOURCE_ZH_ID,
    cn_number, find_tables, inner_zh, load, load_full, parse_table, skill_id, slugify,
    split_annot, split_sections, strip_annotations, zh_to_int,
)

STAGING = ROOT / "data" / "bfrd_staging"

SCHOOL_EN_ZH = {
    "Abjuration": "防护系", "Conjuration": "咒法系", "Divination": "预言系", "Enchantment": "附魔系",
    "Evocation": "塑能系", "Illusion": "幻术系", "Necromancy": "死灵系", "Transmutation": "变化系",
}

CLASS_EN = ["Barbarian", "Bard", "Cleric", "Druid", "Fighter", "Mechanist", "Monk",
            "Paladin", "Ranger", "Rogue", "Sorcerer", "Warlock", "Wizard"]

# 职业主属性（同伴生成/AI 参考用；按 BFRD 推荐）
CLASS_PRIMARY = {
    "Barbarian": ["str", "con"], "Bard": ["cha", "dex"], "Cleric": ["wis", "str"],
    "Druid": ["wis", "con"], "Fighter": ["str", "con"], "Mechanist": ["int", "dex"],
    "Monk": ["dex", "wis"], "Paladin": ["str", "cha"], "Ranger": ["dex", "wis"],
    "Rogue": ["dex", "cha"], "Sorcerer": ["cha", "con"], "Warlock": ["cha", "con"],
    "Wizard": ["int", "con"],
}


def save(name, data):
    STAGING.mkdir(parents=True, exist_ok=True)
    (STAGING / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  → {name}.json（{len(data)} 条）")


def split_respect_parens(text, seps="、"):
    """按分隔符切分但忽略括号内部（如「残暴重击（Brutal Critical，1 颗骰（Die））」）。"""
    parts, buf, depth = [], [], 0
    for ch in str(text):
        if ch == "（":
            depth += 1
        elif ch == "）":
            depth = max(0, depth - 1)
        if ch in seps and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def zh_en_pairs(text):
    """从「名字（English）」式文本提取 [(中文, English)]。"""
    return [(m.group(1).strip(), m.group(2).strip())
            for m in re.finditer(r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z·]*?)（([A-Za-z0-9'’\-\., ]+?)）", str(text))]


def parse_cost_cp(text):
    """「8,000 金币（gp）」/「5 银币（sp）」/「1 铜币（cp）」→ 铜币整数；无法解析返回 None。"""
    text = str(text)
    match = re.search(r"([\d,]+)\s*(铂金|金币|银币|铜币)", text)
    if not match:
        return None
    amount = int(match.group(1).replace(",", ""))
    unit = {"铂金": 1000, "金币": 100, "银币": 10, "铜币": 1}[match.group(2)]
    return amount * unit


def parse_weight(text):
    match = re.search(r"([\d.]+)\s*磅", str(text))
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# 术语表
# ---------------------------------------------------------------------------


def build_glossary():
    print("[术语表] 解析 …")
    lines = load("glossary")
    entries, flat_en_zh, flat_zh_en = [], {}, {}
    section = ""
    for line in lines:
        heading = re.match(r"^## (.+?)（(.+?)）$", line.strip())
        if heading:
            section = heading.group(1)
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        en, zh = cells[0], cells[1]
        if not re.search(r"[A-Za-z]{2}", en) or not zh or en.upper().startswith("ENGLISH"):
            continue
        if re.fullmatch(r"[\s:|-]+", en):
            continue
        note = cells[2] if len(cells) > 2 else ""
        entries.append({"en": en, "zh": zh, "note": note, "section": section})
        flat_en_zh.setdefault(en.lower(), zh)
        flat_zh_en.setdefault(zh, en)
    save("glossary", {"entries": entries, "en_zh": flat_en_zh, "zh_en": flat_zh_en})
    return entries


# ---------------------------------------------------------------------------
# 血统与传承（03）
# ---------------------------------------------------------------------------


def build_lineage():
    print("[血统/传承] 解析 …")
    lines = load("lineage")
    lineages, heritages = {}, {}
    rx_entry = re.compile(r"^[-*]?\s*\*\*_(.+?)。_\*\*\s*(.*)$")
    for title, body in split_sections(lines, r"^### .+（.+ (?:Lineage Traits|Heritage)）$"):
        zh_title, en_title = split_annot(title.lstrip("# ").strip())
        match = re.match(r"^(.*?)\s*(Lineage Traits|Heritage)$", en_title)
        if not match:
            continue
        name_en, kind = match.group(1).strip(), match.group(2)
        name_zh = zh_title.replace("血统特性", "").replace("传承", "").strip() or zh_title

        entries = []
        for line in body:
            m = rx_entry.match(line.strip())
            if m:
                entry_zh, entry_en = split_annot(m.group(1).strip())
                entries.append({"name_zh": entry_zh, "name_en": entry_en, "desc": [m.group(2).strip()]})
            elif entries and line.strip() and not line.strip().startswith("#"):
                entries[-1]["desc"].append(line.strip())
        traits, adaptations, mode = [], [], "traits"
        for entry in entries:
            name = entry["name_zh"]
            if "自然适应" in name:
                mode = "adapt"
                continue
            item = {
                "name": name,
                "name_en": entry["name_en"],
                "desc": strip_annotations(" ".join(entry["desc"])),
            }
            (adaptations if mode == "adapt" else traits).append(item)
        speed_text = " ".join(t["desc"] for t in traits) + " " + " ".join(t["desc"] for t in adaptations)
        speed_match = re.search(r"步行速度\s*为\s*(\d+)", speed_text) or re.search(r"步行速度[^0-9]{0,14}(\d+)\s*尺", speed_text)
        entry = {
            "name": name_zh, "name_en": name_en,
            "speed": int(speed_match.group(1)) if speed_match else 30,
            "bonuses": {},
            "traits_zh": traits,
            "adaptations_zh": adaptations,
        }
        (lineages if kind == "Lineage Traits" else heritages)[slugify(name_en)] = entry
    save("lineage", {"lineages": lineages, "heritages": heritages})


# ---------------------------------------------------------------------------
# 天赋 / 背景（04）
# ---------------------------------------------------------------------------


def build_talents():
    print("[天赋] 解析 …")
    text = load_full("backgrounds")
    # 两条丢失标题的天赋（原文格式事故）补回
    text = re.sub(r"(?m)^(战斗锤炼（Combat Conditioning）|体魄坚韧（Physical Fortitude）)\s*$", r"#### \1", text)
    if "## 天赋（Talents）" not in text:
        print("  ！未找到天赋章节"); return
    talent_text = text.split("## 天赋（Talents）", 1)[1]
    lines = talent_text.split("\n")
    data = {}
    list_map = {"魔法": "magic", "武艺": "martial", "技术": "technical"}
    for title, body in split_sections(lines, r"^### (魔法|武艺|技术)天赋（.+Talents）$"):
        list_zh = re.match(r"^### (魔法|武艺|技术)", title).group(1)
        for sub_title, sub_body in split_sections(body, r"^#### .+（.+）$"):
            name_zh, name_en = split_annot(sub_title.lstrip("# ").strip())
            prerequisite, desc_parts, benefits = "", [], []
            for line in sub_body:
                stripped = line.strip()
                pre = re.match(r"^_先决条件（Prerequisite）：(.+)_$", stripped)
                if pre:
                    prerequisite = strip_annotations(pre.group(1))
                    continue
                if stripped.startswith("- "):
                    benefits.append(strip_annotations(stripped[2:].strip()))
                elif stripped and not stripped.startswith("#"):
                    desc_parts.append(stripped)
            desc = strip_annotations(" ".join(desc_parts))
            data[slugify(name_en)] = {
                "name": name_zh, "name_en": name_en,
                "list": list_map[list_zh],
                "prerequisite_zh": prerequisite,
                "repeatable": ("多次" in (desc + " " + " ".join(benefits)) and "天赋" in (desc + " ".join(benefits))),
                "desc_zh": desc,
                "benefits_zh": benefits,
            }
    save("talents", data)


def build_backgrounds():
    print("[背景] 解析 …")
    text = load_full("backgrounds").split("## 天赋（Talents）", 1)[0]
    lines = text.split("\n")
    data = {}
    for title, body in split_sections(lines, r"^### (罪犯|学者|士兵)（.+）$"):
        name_zh, name_en = split_annot(title.lstrip("# ").strip())
        blob = "\n".join(body)
        skill_choices, skill_count = [], 2
        m = re.search(r"（Skill Proficiencies）：\*\*\s*(.+?)$", blob, re.M)
        if m:
            for zh_name, en in zh_en_pairs(m.group(1)):
                sid = skill_id(en, zh_name)
                if sid and sid not in skill_choices:
                    skill_choices.append(sid)
            cm = re.search(r"选择(.+?)项", m.group(1))
            count = cn_number(cm.group(1)) if cm else None
            if count:
                skill_count = count
        m = re.search(r"（Additional Proficiencies）：\*\*\s*(.+?)$", blob, re.M)
        additional = strip_annotations(m.group(1)) if m else ""
        m = re.search(r"（Equipment）：\*\*\s*(.+?)$", blob, re.M)
        equipment = strip_annotations(m.group(1)) if m else ""
        talent_ids = []
        m = re.search(r"选择一项天赋（talent）[^：]*：(.+?)。", blob)
        if not m:
            m = re.search(r"选择一项[^：]*：(.+?)。", blob)
        if m:
            for _zh, en in zh_en_pairs(m.group(1)):
                if en:
                    talent_ids.append(slugify(en))
        data[slugify(name_en)] = {
            "name": name_zh, "name_en": name_en,
            "skill_choices": skill_choices, "skill_count": skill_count,
            "additional_zh": additional, "equipment_zh": equipment,
            "talent_choices": talent_ids,
        }
    save("backgrounds", data)


# ---------------------------------------------------------------------------
# 角色创建表 / 状态（01、附录）
# ---------------------------------------------------------------------------


def build_chargen():
    print("[创建表] 解析 …")
    lines = load("creation")
    pointbuy, standard = {}, []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and "（SCORE）" in stripped and "（COST）" in stripped:
            rows, _ = parse_table(lines, i)
            for row in rows[1:]:
                for pair in range(0, len(row) - 1, 2):
                    try:
                        score, cost = int(row[pair]), int(row[pair + 1])
                        pointbuy[score] = cost
                    except (ValueError, IndexError):
                        pass
            break
    text = "\n".join(lines)
    m = re.search(r"(\d+)、(\d+)、(\d+)、(\d+)、(\d+)\s*和\s*(\d+)", text)
    if m:
        standard = [int(x) for x in m.groups()]
    total_match = re.search(r"(\d+)\s*点可用于", text) or re.search(r"以\s*(\d+)\s*点", text)
    save("chargen", {
        "pointbuy_costs": pointbuy,
        "pointbuy_total": int(total_match.group(1)) if total_match else 32,
        "standard_array": standard,
    })


def build_conditions():
    print("[状态] 解析 …")
    lines = load("conditions")
    data = {}
    for title, body in split_sections(lines, r"^#{2,4} .+（.+）$"):
        clean = title.lstrip("# ").strip()
        if clean.startswith("表格") or "（Conditions）" in clean:
            continue
        name_zh, name_en = split_annot(clean)
        if not name_en:
            continue
        desc = strip_annotations(" ".join(l.strip() for l in body if l.strip() and not l.strip().startswith("#")))
        if desc:
            data[slugify(name_en)] = {"name": name_zh, "name_en": name_en, "desc_zh": desc}
    save("conditions", data)


# ---------------------------------------------------------------------------
# 法术（08）
# ---------------------------------------------------------------------------


def parse_spell_fields(body_lines):
    """解析 **施放时间（Casting Time）：** 等字段行，返回 (fields, rest_index)。"""
    labels = {
        "施放时间": "casting_time", "射程": "range", "成分": "components", "持续时间": "duration",
    }
    fields = {}
    index = 0
    for i, line in enumerate(body_lines):
        m = re.match(r"^\*\*(施放时间|射程|成分|持续时间)（.+?）：\*\*\s*(.*)$", line.strip())
        if m:
            fields[labels[m.group(1)]] = m.group(2).strip()
            index = i + 1
    return fields, body_lines[index:]


def parse_spell_meta(meta_zh, meta_en, extra=""):
    """元数据行 → (level, sources, school_zh, ritual)。extra 为斜体行外附的学派段。"""
    level = None
    m = re.search(r"(\d+)(?:st|nd|rd|th)-Circle", meta_en)
    if m:
        level = int(m.group(1))
    elif "Cantrip" in meta_en:
        level = 0
    else:
        m = re.search(r"(\d+)\s*环", meta_zh)
        level = int(m.group(1)) if m else (0 if "戏法" in meta_zh else None)
    sources = []
    for word in re.findall(r"(Arcane|Divine|Primordial|Wyrd)", meta_en):
        sid = SOURCE_EN_ID[word.lower()]
        if sid not in sources:
            sources.append(sid)
    school = ""
    m = re.search(r"\(([A-Za-z]+)\)", meta_en)
    if m:
        school = SCHOOL_EN_ZH.get(m.group(1), m.group(1))
    if not school and extra:
        m = re.search(r"（([A-Za-z]+)）", extra)
        if m:
            school = SCHOOL_EN_ZH.get(m.group(1), m.group(1))
        else:
            m = re.search(r"(\S+?系)", extra)
            if m:
                school = m.group(1)
    if not school:
        m = re.search(r"·(\S+?系)", meta_zh)
        if m:
            school = m.group(1)
    return level, sources, school, ("Ritual" in meta_en)


def spell_damage(desc_zh, higher_zh, level):
    """从中文描述提取结构化伤害（骰/类型/豁免/攻击/治疗）与升环表。"""
    text = desc_zh
    out = {}
    m = re.search(r"(近战|远程)法术攻击", text)
    if m:
        out["attack"] = f"{m.group(1)}法术攻击"
    m = re.search(r"的(力量|敏捷|体质|智力|感知|魅力)（(STR|DEX|CON|INT|WIS|CHA)）豁免", text)
    if not m:
        m = re.search(r"(力量|敏捷|体质|智力|感知|魅力)（(STR|DEX|CON|INT|WIS|CHA)）豁免", text)
    if m:
        out["save"] = ABILITY_ZH_ID.get(m.group(1), ABILITY_ABBR_ID.get(m.group(2), ""))
        out["save_success"] = "half" if ("一半" in text or "半数" in text) else "none"
    m = re.search(r"(\d+d\d+(?:\s*\+\s*\d+)?)\s*点([\u4e00-\u9fff]{2})伤害", text)
    if m and m.group(2) in DAMAGE_ZH:
        out["type"] = m.group(2)
        out["dice"] = m.group(1).replace(" ", "")
    # 戏法随角色等级成长（BFRD 统一句式：「5 级（5th level）时……增加 1dX（2dX），11 级（3dX）、17 级（4dX）……」）
    if level == 0 and out.get("dice"):
        growth = {
            int(gm.group(1)): gm.group(2)
            for gm in re.finditer(r"(\d+)\s*级（(\d+d\d+)）", text)
        }
        # 「达到 5 级（5th level）时……增加 1dX（2dX）」：首个成长点在括注增量里
        gm5 = re.search(r"达到\s*(\d+)\s*级[^。]*?增加\s*\d+d\d+（(\d+d\d+)）", text)
        if gm5:
            growth[int(gm5.group(1))] = gm5.group(2)
        if growth:
            by_level = {"1": out["dice"]}
            for lv in sorted(growth):
                by_level[str(lv)] = growth[lv]
            out["by_char_level"] = by_level
    m = re.search(r"恢复(?:了)?\s*(\d+d\d+(?:\s*\+\s*\d+)?|\d+)\s*点?生命值", text)
    if m:
        out["heal"] = m.group(1).replace(" ", "")
    # 多弹体升环（魔法飞弹/灼热射线式）：「高于 N 环的每一环额外创造 M 枚/道」→ 总量升环表
    mm = re.search(r"高于\s*(\d+)\s*环的每一环额外创造\s*([一二三四五六七八九十\d]+)\s*[枚道个]", higher_zh)
    if mm and out.get("dice"):
        cm = re.search(r"创造\s*([一二三四五六七八九十\d]+)\s*[枚道个]", text)
        per = re.match(r"(\d+)d(\d+)(?:\s*\+\s*(\d+))?", out["dice"].replace(" ", ""))
        if cm and per:
            base_n = zh_to_int(cm.group(1))
            over, inc = int(mm.group(1)), zh_to_int(mm.group(2))
            faces, flat = per.group(2), int(per.group(3) or 0)
            if base_n and inc:
                by_slot = {}
                for slot in range(max(1, level or 1), 10):
                    n = base_n + max(0, slot - over) * inc
                    by_slot[str(slot)] = f"{n}d{faces}" + (f" + {flat * n}" if flat else "")
                out["by_slot"] = by_slot
    # 升环表：常见模式「高于 N 环的每一环…增加 1d4」/「每提高 1 环…增加 1d6」
    m = re.search(r"高于\s*(\d+)\s*环的每一环.*?增加\s*(\d+)d(\d+)", higher_zh)
    if m:
        over, inc_count, inc_sides = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"环[^。]{0,26}?增加\s*(\d+)d(\d+)", higher_zh)
        over = level if level is not None else 1
        inc_count, inc_sides = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    if inc_count and out.get("dice") and level is not None:
        base = re.match(r"(\d+)d(\d+)", out["dice"])
        if base and int(base.group(2)) == inc_sides:
            by_slot = {}
            for slot in range(max(1, level), 10):
                count = int(base.group(1)) + (slot - over) * inc_count
                if count >= int(base.group(1)):
                    by_slot[str(slot)] = f"{count}d{inc_sides}"
            if by_slot:
                out["by_slot"] = by_slot
    return out or None


def parse_spell_block(body):
    """一个法术正文（元数据行之后）→ 结构化 dict。"""
    meta_zh, meta_en, meta_extra = None, None, ""
    rest = []
    for i, line in enumerate(body):
        m = re.match(r"^_(.+?)（(.+?)）_\s*(?:（(.+?)）)?$", line.strip())
        if m and meta_zh is None and ("环" in m.group(1) or "戏法" in m.group(1)):
            meta_zh, meta_en, meta_extra = m.group(1), m.group(2), m.group(3) or ""
            rest = body[i + 1:]
            break
    if meta_zh is None:
        return None
    level, sources, school, ritual = parse_spell_meta(meta_zh, meta_en, meta_extra)
    if level is None:
        return None
    fields, content = parse_spell_fields(rest)
    desc_parts, higher_parts, in_higher = [], [], False
    for line in content:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("_更高环阶"):
            in_higher = True
            remainder = re.sub(r"^_更高环阶（.+?）。_\s*", "", stripped)
            if remainder:
                higher_parts.append(remainder)
            continue
        if stripped.startswith("#"):
            break
        (higher_parts if in_higher else desc_parts).append(stripped)
    # 内容里可能混着下一个 ###/#### 标题？split_sections 已保证块边界
    desc = strip_annotations("\n".join(desc_parts))
    higher = strip_annotations(" ".join(higher_parts))
    # 施放时间/射程/持续时间等字段值也清括注
    casting = strip_annotations(fields.get("casting_time", ""))
    range_text = strip_annotations(fields.get("range", ""))
    duration_raw = fields.get("duration", "")
    concentration = "专注" in duration_raw
    duration = strip_annotations(duration_raw)
    rounds = 0
    m = re.search(r"(\d+)\s*(轮|分钟|小时|天)", duration)
    if m:
        unit = {"轮": 1, "分钟": 10, "小时": 600, "天": 14400}[m.group(2)]
        rounds = int(m.group(1)) * unit
    comp_raw = fields.get("components", "")
    components, material = [], ""
    for part in re.split(r"[、,，]", comp_raw):
        part = part.strip()
        if part.startswith("V"):
            components.append("言语")
        elif part.startswith("S"):
            components.append("姿势")
        elif part.startswith("M"):
            components.append("材料")
            mm = re.search(r"M（(.+?)）", part)
            if mm:
                material = strip_annotations(mm.group(1))
    return {
        "level": level, "school": school, "sources": sources, "ritual": ritual,
        "casting_time": casting, "range": range_text,
        "components": components, "material": material,
        "duration": duration, "concentration": concentration, "duration_rounds": rounds,
        "damage": spell_damage(desc, higher, level),
        "desc": desc, "higher_level": higher,
    }


def build_spells():
    print("[法术] 解析 …")
    text = load_full("spellcasting")
    data = {}
    for area, ritual in (("法术描述", False), ("仪式描述", True)):
        marker = f"## {area}（"
        if marker not in text:
            marker = None
        section = text.split("## 法术描述（Spell Descriptions）", 1)[-1] if area == "法术描述" else text.split("## 仪式描述（Ritual Descriptions）", 1)[-1]
        if area == "法术描述":
            section = section.split("## 仪式描述（Ritual Descriptions）", 1)[0]
        lines = section.split("\n")
        rx = r"^#### .+（.+）$" if not ritual else r"^### .+（.+）$"
        for title, body in split_sections(lines, rx):
            name_zh, name_en = split_annot(title.lstrip("# ").strip())
            if not name_en:
                continue
            parsed = parse_spell_block(body)
            if not parsed:
                print(f"  ！跳过（结构异常）：{title}")
                continue
            parsed["ritual"] = parsed["ritual"] or ritual
            parsed["name"] = name_zh
            parsed["name_en"] = name_en
            data[slugify(name_en)] = parsed
    save("spells", data)


# ---------------------------------------------------------------------------
# 装备与魔法物品（06）
# ---------------------------------------------------------------------------


def nearest_heading(lines, index, level=4, depth=80):
    """向上找最近的指定级别标题文本。"""
    for i in range(index - 1, max(0, index - depth), -1):
        m = re.match(rf"^#{{{level},6}} (.+)$", lines[i].strip())
        if m:
            return m.group(1)
    return ""


def build_items():
    print("[物品] 解析 …")
    lines = load("equipment")
    data = {}

    for start, rows in find_tables(lines):
        header = " ".join(rows[0])
        info = {"cost_cp": None, "weight": None}
        try:
            if "护甲（ARMOR）" in header:
                parent = nearest_heading(lines, start, 4) or nearest_heading(lines, start, 3)
                cat = next((c for c in ("轻甲", "中甲", "重甲", "盾牌") if c in parent), "护甲")
                for row in rows[1:]:
                    if len(row) < 2 or not row[0] or row[0].startswith("_") or row[0].startswith("#"):
                        continue
                    name_zh, name_en = split_annot(row[0])
                    if not name_en:
                        continue
                    entry = {
                        "name": name_zh, "name_en": name_en, "category": "Armor", "category_zh": cat,
                        "cost_cp": parse_cost_cp(row[1]) if len(row) > 1 else None, "price_note": "", "weight": None,
                        "rarity_zh": "", "desc_zh": "", "armor_category_zh": cat,
                        "ac_text": inner_zh(row[2]) if len(row) > 2 else "",
                    }
                    ac_text = entry["ac_text"]
                    m_ac = re.match(r"^(\d+)(?:\s*\+\s*敏捷调整值(?:（上限\s*(\d+)）)?)?", ac_text)
                    if m_ac and m_ac.group(1):
                        entry["ac_base"] = int(m_ac.group(1))
                        if m_ac.group(2):
                            entry["ac_dex"] = int(m_ac.group(2))
                        elif "敏捷调整值" in ac_text:
                            entry["ac_dex"] = "full"
                        else:
                            entry["ac_dex"] = 0
                    for cell in row[2:]:
                        if "重量" in header and re.search(r"[\d.]+\s*磅", cell):
                            entry["weight"] = parse_weight(cell)
                        if re.fullmatch(r"\d+", inner_zh(cell).strip()):
                            entry["str_minimum"] = int(inner_zh(cell).strip())
                        if "劣势" in cell:
                            entry["stealth_disadvantage"] = True
                    data[slugify(name_en)] = entry
            elif "武器（WEAPON）" in header:
                col = {}
                for idx, cell in enumerate(rows[0]):
                    if "花费" in cell:
                        col["cost"] = idx
                    if "伤害" in cell:
                        col["damage"] = idx
                    if "重量" in cell:
                        col["weight"] = idx
                    if "武器选项" in cell:
                        col["option"] = idx
                    if "属性" in cell:
                        col["props"] = idx
                parent = nearest_heading(lines, start, 4) or nearest_heading(lines, start, 3)
                cat = ("简易" if "简易" in parent else ("军用" if "军用" in parent else "")) + \
                      ("远程" if "远程" in parent else ("近战" if "近战" in parent else ""))
                for row in rows[1:]:
                    if len(row) < 2 or not row[0] or row[0].startswith("_") or row[0].startswith("#"):
                        continue
                    name_zh, name_en = split_annot(row[0])
                    if not name_en:
                        continue
                    entry = {
                        "name": name_zh, "name_en": name_en, "category": "Weapon", "category_zh": "武器",
                        "weapon_category_zh": cat or "武器",
                        "cost_cp": parse_cost_cp(row[col["cost"]]) if "cost" in col and col["cost"] < len(row) else None,
                        "price_note": "", "weight": None,
                        "rarity_zh": "", "desc_zh": "",
                        "damage": "", "damage_type_zh": "", "properties_zh": [], "weapon_options_zh": [],
                    }
                    if "damage" in col and col["damage"] < len(row):
                        dmg_cell = row[col["damage"]]
                        dices = re.findall(r"\d+d\d+", dmg_cell)
                        if dices:
                            entry["damage"] = dices[0]
                        else:
                            fixed = re.match(r"\s*(\d+)\s", dmg_cell)   # 固定伤害（如吹箭筒"1 穿刺"）
                            if fixed:
                                entry["damage"] = fixed.group(1)
                        if len(dices) > 1:
                            entry["versatile_damage"] = dices[1]
                        tm = re.search(r"([\u4e00-\u9fff]{2})（", dmg_cell)
                        if tm and tm.group(1) in DAMAGE_ZH:
                            entry["damage_type_zh"] = tm.group(1)
                    if "weight" in col and col["weight"] < len(row):
                        entry["weight"] = parse_weight(row[col["weight"]])
                    if "option" in col and col["option"] < len(row):
                        entry["weapon_options_zh"] = [p for p in (strip_annotations(x) for x in re.split(r"[、,]", row[col["option"]])) if p and p != "-"]
                    if "props" in col and col["props"] < len(row):
                        entry["properties_zh"] = [p for p in (strip_annotations(x) for x in re.split(r"[、,]", row[col["props"]])) if p and p != "-"]
                    data[slugify(name_en)] = entry
            elif "花费（COST）" in header or "成本（COST）" in header:
                parent = nearest_heading(lines, start, 3) or nearest_heading(lines, start, 4)
                cat = re.sub(r"^表格：", "", re.sub(r"（.+?）", "", parent)).strip() or "冒险装备"
                for row in rows[1:]:
                    if len(row) < 2 or not row[0]:
                        continue
                    if row[0].startswith("_") or row[0].startswith("#"):
                        continue
                    name_zh, name_en = split_annot(row[0])
                    if not name_en:
                        continue
                    entry = {
                        "name": name_zh, "name_en": name_en,
                        "category": "Gear", "category_zh": cat,
                        "cost_cp": parse_cost_cp(row[1]), "price_note": "", "weight": None,
                        "rarity_zh": "", "desc_zh": "",
                    }
                    for cell in row[1:]:
                        if re.search(r"[\d.]+\s*磅", cell):
                            entry["weight"] = parse_weight(cell)
                    data[slugify(name_en)] = entry
        except Exception as exc:
            print(f"  ！表格解析异常（行 {start}）：{exc}")

    # 魔法物品
    magic_marker = "## 魔法物品（Magic Items）"
    if magic_marker in load_full("equipment"):
        magic_text = load_full("equipment").split(magic_marker, 1)[1]
        magic_lines = magic_text.split("\n")
        count = 0
        for title, body in split_sections(magic_lines, r"^#### .+（.+）$"):
            name_zh, name_en = split_annot(title.lstrip("# ").strip())
            if not name_en or "表格" in name_zh:
                continue
            meta, desc_parts = "", []
            for line in body:
                stripped = line.strip()
                if not meta and re.match(r"^_.+_$", stripped) and "（" in stripped:
                    meta = stripped.strip("_")
                    continue
                if stripped and not stripped.startswith("#"):
                    desc_parts.append(stripped)
            rarity_zh = next((r for r in ("非常稀有", "传奇", "非普通", "普通", "稀有") if r in meta), "")
            cost_cp = parse_cost_cp(meta)
            price_note = "" if cost_cp is not None else "价格不定"
            type_zh = strip_annotations(meta.split("，")[0]).strip() if meta else ""
            data[slugify(name_en)] = {
                "name": name_zh, "name_en": name_en,
                "category": "Magic", "category_zh": type_zh or "魔法物品",
                "cost_cp": cost_cp, "price_note": price_note, "weight": None,
                "rarity_zh": rarity_zh,
                "attunement": ("调谐" in meta or "Attunement" in meta),
                "desc_zh": strip_annotations("\n".join(desc_parts)),
            }
            count += 1
        print(f"  魔法物品 {count} 条")
    # 载具（Vehicles）：基础花费表 + 各载具数据块（文本说明；不参与战斗机制，供 GM/AI 引用）
    if "## 载具（Vehicles）" in load_full("equipment"):
        veh_text = load_full("equipment").split("## 载具（Vehicles）", 1)[1]
        veh_lines = veh_text.split("\n")
        stats = {}
        stat_idx = [i for i, line in enumerate(veh_lines) if re.match(r"^#{4,5} 数据块：.+（.+）$", line.strip())]
        for pos, start_idx in enumerate(stat_idx):
            title = veh_lines[start_idx].strip()
            clean_title = re.sub(r"^#{4,5}\s*数据块：", "", title).strip()
            name_zh, name_en = split_annot(clean_title)
            name_en = re.sub(r"^Stat Block[:：]\s*", "", name_en).strip()   # 「数据块：X（Stat Block: X）」→ 取 X
            if not name_en:
                continue
            # 正文止于下一个任意级别的标题（数据块、载具描述或下一章节）
            body = []
            for line in veh_lines[start_idx + 1:]:
                if re.match(r"^#{1,5} ", line.strip()):
                    break
                body.append(line)
            text_lines = []
            for line in body:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if re.match(r"^\|\s*(STR|DEX|CON|INT|WIS|CHA)\s*\|", s):
                    continue                                    # 属性表头行略过，值行随后解析
                if re.match(r"^\|[\s:|−-]+\|$", s):
                    continue                                    # 表格分隔行（|---|）略过
                mval = re.match(r"^\|\s*([+-−]?\d+|[-－])\s*\|\s*([+-−]?\d+|[-－])\s*\|\s*([+-−]?\d+|[-－])\s*\|\s*([+-−]?\d+|[-－])\s*\|\s*([+-−]?\d+|[-－])\s*\|\s*([+-−]?\d+|[-－])\s*\|$", s)
                if mval:
                    vals = [v.strip() for v in mval.groups()]
                    text_lines.append(f"属性值：STR {vals[0]}、DEX {vals[1]}、CON {vals[2]}、INT {vals[3]}、WIS {vals[4]}、CHA {vals[5]}")
                    continue
                text_lines.append(s)
            text = strip_annotations("\n".join(text_lines))
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)        # 去粗体标记
            text = re.sub(r"_(.+?)_", r"\1", text)              # 去斜体标记
            stats[name_en] = text.strip()
        veh_count = 0
        for start, rows in find_tables(veh_lines):
            header = " ".join(rows[0])
            if "载具（VEHICLE）" not in header:
                continue
            for row in rows[1:]:
                if len(row) < 3 or not row[0]:
                    continue
                if row[0].startswith("_") or row[0].startswith("#"):
                    continue
                name_zh, name_en = split_annot(row[0])
                if not name_en:
                    continue
                data[slugify(name_en)] = {
                    "name": name_zh, "name_en": name_en,
                    "category": "Vehicle", "category_zh": "载具",
                    "cost_cp": parse_cost_cp(row[1]), "price_note": f"旅行速度 {strip_annotations(row[2]).strip()}",
                    "weight": None, "rarity_zh": "", "desc_zh": stats.get(name_en, ""),
                }
                veh_count += 1
        print(f"  载具 {veh_count} 条")
    # 盾牌标记与基础值（普通盾 +2；魔法盾牌同样属于盾类，基础加值按 +2 起算）
    shield_count = 0
    for entry in data.values():
        name = str(entry.get("name") or "")
        if "盾" not in name and "盾" not in str(entry.get("category_zh") or ""):
            continue
        entry["is_shield"] = True
        entry.setdefault("ac_base", 2)
        entry.setdefault("ac_dex", 0)
        shield_count += 1
    if shield_count:
        print(f"  盾牌 {shield_count} 条")
    save("items", data)


# ---------------------------------------------------------------------------
# 怪物（09）
# ---------------------------------------------------------------------------


def build_gods():
    """神祇与神系（05）：三个神系的导语 + 神祇表（名/称号/领域/圣徽）。"""
    print("[神祇] 解析 …")
    lines = load("gods")
    data = {}
    for title, body in split_sections(lines, r"^### .+（.+ Pantheon）$"):
        name_zh, name_en = split_annot(title.lstrip("# ").strip())
        intro_lines = []
        for line in body:
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("|"):
                continue
            intro_lines.append(strip_annotations(s))
        deities = []
        for start, rows in find_tables(body):
            if "神祇（DEITY）" not in " ".join(rows[0]):
                continue
            for row in rows[1:]:
                if not row or not row[0]:
                    continue
                m = re.match(r"^(.+?)（(.+?)）(?:[，,](.*))?$", row[0].strip())
                if not m:
                    continue
                domains = []
                if len(row) > 1:
                    domains = [strip_annotations(d).strip() for d in re.split(r"[、,，]", row[1]) if d.strip()]
                deities.append({
                    "name": m.group(1).strip(),
                    "name_en": m.group(2).strip(),
                    "title": (m.group(3) or "").strip(),
                    "domains": domains,
                    "symbol": strip_annotations(row[2]).strip() if len(row) > 2 else "",
                })
        data[slugify(name_en)] = {
            "name": name_zh, "name_en": name_en,
            "desc": " ".join(intro_lines),
            "deities": deities,
        }
        print(f"  {name_zh}：{len(deities)} 位神祇")
    save("gods", data)


def build_monsters():
    print("[怪物] 解析 …")
    lines = load("monsters")
    # XP / PB 表
    xp_map, pb_map = {}, {}
    for start, rows in find_tables(lines):
        header = " ".join(rows[0])
        if "经验值" in header and start < 120:
            for row in rows[1:]:
                if len(row) >= 2:
                    xp_map[inner_zh(row[0]).strip()] = inner_zh(row[1]).strip()
        if "熟练加值" in header and start < 120:
            for row in rows[1:]:
                if len(row) >= 2:
                    pb_map[inner_zh(row[0]).strip()] = inner_zh(row[1]).strip()

    data = {}
    rx_block = re.compile(r"^#{3,4} (.+?)（(.+?)）（CR (.+?)）$")
    sections = split_sections(lines, r"^#{3,4} .+（.+）（CR .+）$")
    for title, body in sections:
        m = rx_block.match(title)
        name_zh, name_en, cr_label = m.group(1), m.group(2), m.group(3).strip()
        cr = {"1/8": 0.125, "1/4": 0.25, "1/2": 0.5, "0": 0.0}.get(cr_label)
        if cr is None:
            try:
                cr = float(cr_label)
            except ValueError:
                cr = 0.0
        entry = {
            "name": name_zh, "name_en": name_en,
            "cr": cr, "cr_label": cr_label,
            "xp": int(xp_map.get(cr_label, "0").replace(",", "") or 0),
            "pb": pb_map.get(cr_label, ""),
            "ac": 10, "hp": 1, "speed": 30, "speed_text": "", "init_bonus": 0,
            "size_zh": "", "type_zh": "",
            "perception": None, "stealth": None,
            "senses_zh": "", "defenses_zh": [], "languages_zh": "",
            "ability_mods": {},
            "abilities_zh": [], "actions_zh": [], "bonus_zh": [], "reactions_zh": [],
            "legendary_zh": [], "lair_zh": [], "regional_zh": [],
            "attack": {}, "traits_zh": [],
        }
        section_map = {
            "动作": "actions_zh", "附赠动作": "bonus_zh", "反应": "reactions_zh",
            "传奇动作": "legendary_zh", "巢穴动作": "lair_zh", "区域效果": "regional_zh",
        }
        current = "abilities_zh"
        rx_field = re.compile(r"^- \*\*(护甲等级|生命值|速度|察觉|隐匿|抗性|免疫|易伤|感官|语言)（.+?）\*\*\s*(.+)$")
        rx_trait = re.compile(r"^[-•]\s*\*\*_?(.+?)（(.+?)）。_?\*\*\s*(.*)$")
        rx_heading = re.compile(r"^#{3,5} (动作|附赠动作|反应|传奇动作|巢穴动作|区域效果)（.+Actions?|.*?）$")
        pending = None
        type_line_taken = False
        mods_taken = False
        for line in body:
            stripped = line.strip()
            if not stripped:
                continue
            hm = re.match(r"^#{3,5} (动作|附赠动作|反应|传奇动作|巢穴动作|区域效果)（", stripped)
            if hm:
                current = section_map[hm.group(1)]
                pending = None
                continue
            if stripped.startswith("#"):
                pending = None
                continue
            fm = rx_field.match(stripped)
            if fm:
                label, value = fm.group(1), fm.group(2).strip()
                pending = None
                if label == "护甲等级":
                    num = re.search(r"\d+", value)
                    entry["ac"] = int(num.group(0)) if num else 10
                elif label == "生命值":
                    num = re.search(r"\d+", value)
                    entry["hp"] = int(num.group(0)) if num else 1
                elif label == "速度":
                    entry["speed_text"] = strip_annotations(value)
                    num = re.search(r"(\d+)\s*尺", value)
                    entry["speed"] = int(num.group(1)) if num else 30
                elif label == "察觉":
                    num = re.search(r"(\d+)", value)
                    entry["perception"] = int(num.group(1)) if num else None
                elif label == "隐匿":
                    num = re.search(r"(\d+)", value)
                    entry["stealth"] = int(num.group(1)) if num else None
                elif label in ("抗性", "免疫", "易伤"):
                    entry["defenses_zh"].append(f"{label}：{strip_annotations(value)}")
                elif label == "感官":
                    entry["senses_zh"] = strip_annotations(value)
                elif label == "语言":
                    entry["languages_zh"] = strip_annotations(value)
                continue
            tm = rx_trait.match(stripped)
            if tm:
                item = {"name": tm.group(1).strip(), "name_en": tm.group(2).strip(), "desc": tm.group(3).strip()}
                entry[current].append(item)
                pending = item
                continue
            if re.fullmatch(r"\*\*_?（?每日.*", stripped) is None and pending and stripped:
                # 特质子弹的延续行（跨行描述）
                if not stripped.startswith("|"):
                    pending["desc"] = (pending["desc"] + " " + stripped).strip()
                continue
            if stripped.startswith("|"):
                # 属性调整值表
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if len(cells) == 6 and all(re.fullmatch(r"[+-]\d+", c) for c in cells):
                    if not mods_taken:
                        mods = [int(c) for c in cells]
                        entry["ability_mods"] = {"str": mods[0], "dex": mods[1], "con": mods[2], "int": mods[3], "wis": mods[4], "cha": mods[5]}
                        entry["init_bonus"] = mods[1]
                        mods_taken = True
                continue
            if not type_line_taken and stripped.startswith("_") and "（" in stripped:
                cleaned = strip_annotations(stripped.strip("_"))
                sizes = ["超巨型", "巨型", "微型", "小型", "中型", "大型"]
                for size in sizes:
                    if cleaned.startswith(size):
                        entry["size_zh"] = size
                        entry["type_zh"] = cleaned[len(size):].strip()
                        break
                else:
                    entry["type_zh"] = cleaned
                type_line_taken = True
                continue
        # 清理特质描述中的括注 + 主攻击
        for key in ("abilities_zh", "actions_zh", "bonus_zh", "reactions_zh", "legendary_zh", "lair_zh", "regional_zh"):
            for item in entry[key]:
                item["desc"] = strip_annotations(item["desc"])
        for item in entry["actions_zh"]:
            hit = re.search(r"\+(\d+)\s*命中", item["desc"])
            dm = re.search(r"(\d+d\d+(?:\s*\+\s*\d+)?)\s*）?\s*点(.{0,90})", item["desc"])
            if hit and dm:
                segment = dm.group(2)
                dtype, best = "", 10 ** 6
                for t in DAMAGE_ZH:
                    pos = segment.find(t)
                    if pos != -1 and pos < best:
                        best, dtype = pos, t
                if dtype:
                    entry["attack"] = {"name": item["name"], "to_hit": int(hit.group(1)), "damage": f"{dm.group(1).replace(' ', '')} {dtype}"}
                    break
        # 展示用 traits 列
        traits = ["速度：" + (entry["speed_text"] or f"{entry['speed']} 尺")]
        if entry["perception"] is not None or entry["stealth"] is not None:
            bits = []
            if entry["perception"] is not None:
                bits.append(f"察觉 {entry['perception']}")
            if entry["stealth"] is not None:
                bits.append(f"隐匿 {entry['stealth']}")
            traits.append("、".join(bits))
        traits += entry["defenses_zh"]
        if entry["senses_zh"]:
            traits.append("感官：" + entry["senses_zh"])
        if entry["languages_zh"]:
            traits.append("语言：" + entry["languages_zh"])
        for key, prefix in (("abilities_zh", ""), ("actions_zh", ""), ("bonus_zh", "附赠动作——"),
                            ("reactions_zh", "反应——"), ("legendary_zh", "传奇动作——"),
                            ("lair_zh", "巢穴动作——"), ("regional_zh", "区域效果——")):
            for item in entry[key]:
                traits.append(f"{prefix}{item['name']}：{item['desc']}")
        entry["traits_zh"] = traits
        # 清理空字段
        for key in ("abilities_zh", "actions_zh", "bonus_zh", "reactions_zh", "legendary_zh", "lair_zh", "regional_zh"):
            entry[key] = [i for i in entry[key] if i.get("desc") or key in ("abilities_zh", "actions_zh")]
        data[slugify(name_en)] = entry
    save("monsters", data)


# ---------------------------------------------------------------------------
# 职业（02）
# ---------------------------------------------------------------------------


def class_features_from_body(body_lines):
    """从职业正文提取特性块：{slug: {name, name_en, level, marker, desc}} 与有序列表。"""
    features, order = {}, []
    rx_head = re.compile(r"^#{3,4} (.+?)（(.+?)）$")
    rx_marker = re.compile(r"^_(.+?)（(.+?Level .+?Feature)）_$")
    i = 0
    n = len(body_lines)
    while i < n:
        head = rx_head.match(body_lines[i].strip())
        if head and not head.group(1).startswith("表格") and "Subclass" not in head.group(2):
            name_zh, name_en = head.group(1).strip(), head.group(2).strip()
            marker = None
            j = i + 1
            while j < min(i + 5, n) and not body_lines[j].strip():
                j += 1
            if j < n:
                mm = rx_marker.match(body_lines[j].strip())
                if mm:
                    marker = (mm.group(1), mm.group(2))
                    i = j + 1
                    desc_lines = []
                    while i < n:
                        stripped = body_lines[i].strip()
                        # 只被 2-4 级标题或下一个特性标记截断；#####/###### 属于特性内子块（如施法属性的子章）
                        if re.match(r"^#{2,4} ", stripped) or rx_marker.match(stripped):
                            break
                        if stripped:
                            desc_lines.append(stripped)
                        i += 1
                    level = None
                    zh_nums = [int(x) for x in re.findall(r"(\d+)", marker[0])]
                    if zh_nums:
                        level = min(zh_nums)                     # 多档标记「2、13和 18 级」→ 首档（该特性的获得等级）
                    else:
                        en_nums = [int(x) for x in re.findall(r"(\d+)(?:st|nd|rd|th)", marker[1])]
                        level = min(en_nums) if en_nums else None
                    slug = slugify(name_en)
                    features[slug] = {
                        "name": name_zh, "name_en": name_en, "level": level,
                        "marker_zh": strip_annotations(marker[0]),
                        "desc": strip_annotations("\n".join(desc_lines)),
                        "desc_raw": "\n".join(desc_lines),   # 带括注原文（仅供解析属性/来源，落盘前移除）
                    }
                    order.append(slug)
                    continue
        i += 1
    return features, order


def _class_table_key(cell):
    """职业进阶表表头 → 列键（通用列 + 职业专属列）。无法识别返回空串。"""
    text = inner_zh(cell)
    if "等级" in text:
        return "level"
    if "熟练加值" in text:
        return "pb"
    if "特性" in text:
        return "features"
    if "狂暴伤害" in text:
        return "rage_damage"
    if "狂暴" in text:
        return "rages"
    if "野兽形态" in text:
        return "beast_forms"
    if "已知强化效果" in text:
        return "augment_effects"
    if "已强化物品" in text:
        return "augmented_items"
    if "武艺" in text:
        return "martial_arts"
    if "技巧点" in text:
        return "technique_points"
    if "无甲移动" in text:
        return "unarmored_movement"
    if "偷袭" in text:
        return "sneak_attack"
    if "术法点" in text:
        return "sorcery_points"
    if "祈唤" in text:
        return "invocations"
    if "已知戏法" in text:
        return "cantrips"
    if "已知仪式" in text:
        return "rituals"
    if "已知法术" in text:
        return "spells"
    if "准备" in text:
        return "prepared"
    m = re.match(r"(\d)\s*环", text)
    if m:
        return f"slot_{m.group(1)}"
    return ""


def build_classes():
    print("[职业] 解析 …")
    lines = load("classes")
    data = {}
    class_rx = r"^## .+（(?:Barbarian|Bard|Cleric|Druid|Fighter|Mechanist|Monk|Paladin|Ranger|Rogue|Sorcerer|Warlock|Wizard)）$"
    for title, body in split_sections(lines, class_rx):
        name_zh, name_en = split_annot(title.lstrip("# ").strip())
        if name_en not in CLASS_EN:
            continue
        blob = "\n".join(body)
        entry = {
            "name": name_zh, "name_en": name_en,
            "hit_die": 8, "saves": [], "skill_choices": [], "skill_count": 2,
            "armor_zh": "", "weapons_zh": "", "tools_zh": "",
            "equipment_lines": [], "primary": CLASS_PRIMARY.get(name_en, []),
            "prof_bonus": 2, "spell_ability": None, "sources": [],
            "improvement_talents": [],
            "features": {}, "feature_order": [],
            "table": {}, "slots": {}, "cantrips_known": {}, "spells_known": {}, "prepared": {}, "rituals_known": {},
            "subclasses": {},
        }
        m = re.search(r"生命骰（Hit Dice）：\*\*\s*每个.+?(\d+)d(\d+)", blob) or re.search(r"(\d+)d(\d+)\s*每个", blob)
        if m:
            entry["hit_die"] = int(m.group(2))
        m = re.search(r"豁免（Saves）：\*{0,2}\s*([A-Z]{3}(?:、[A-Z]{3})*)", blob)
        if m:
            for abbr in re.findall(r"[A-Z]{3}", m.group(1)):
                aid = ABILITY_ABBR_ID.get(abbr)
                if aid and aid not in entry["saves"]:
                    entry["saves"].append(aid)
        m = re.search(r"技能（Skills）：\*{0,2}\s*(.+?)\s*$", blob, re.M)
        if m:
            value = m.group(1)
            if "任意" in value:
                entry["skill_choices"] = "any"          # 「任意三项你自选的技能」（如吟游诗人）
            else:
                for zh_name, en in zh_en_pairs(value):
                    sid = skill_id(en, zh_name)
                    if sid and sid not in entry["skill_choices"]:
                        entry["skill_choices"].append(sid)
            cm = re.search(r"(?:选择|任意)(.+?)项", value)
            count = cn_number(cm.group(1)) if cm else None
            if count:
                entry["skill_count"] = count
        for label, key in (("护甲（Armor）", "armor_zh"), ("武器（Weapons）", "weapons_zh"), ("工具（Tools）", "tools_zh")):
            m = re.search(rf"{label}：\*{{0,2}}\s*(.+?)\s*$", blob, re.M)
            if m:
                entry[key] = strip_annotations(m.group(1))
        # 初始装备
        m = re.search(r"初始装备（Starting Equipment）\n+(.+?)(?=\n#{3,4} |\n## |\Z)", blob, re.S)
        if m:
            for line in m.group(1).split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    if line.startswith(("- ", "* ")):
                        line = line[2:]
                    line = strip_annotations(line)
                    if line and not line.endswith("："):     # 跳过"你初始拥有以下装备："类导语
                        entry["equipment_lines"].append(line)
        # 特性
        # 本职特性扫描：先裁掉"真实子职业"区段（排除泛用「X 子职业」节，其内仍含本职特性）；
        # 译文偶有排布异常，随后再用标记（marker 含子职业名）双向归位兜底。
        subclass_starts = []
        for i, line in enumerate(body):
            m = re.match(r"^#{2,4} (.+?)（(.+? Subclass)）\s*$", line.strip())
            if m and m.group(2).strip() != f"{name_en} Subclass":
                subclass_starts.append(i)
        if subclass_starts:
            bounds = subclass_starts + [len(body)]
            skip = []
            for a, b in zip(bounds[:-1], bounds[1:]):
                skip.append((a, b))
            base_body = [line for i, line in enumerate(body) if not any(a <= i < b for a, b in skip)]
        else:
            base_body = body
        features, order = class_features_from_body(base_body)
        entry["features"], entry["feature_order"] = features, order
        # 施法：从"施法（Spellcasting）"特性正文解析施法属性与法术来源
        sc_feat = features.get("spellcasting")
        if sc_feat:
            sc = sc_feat.get("desc_raw") or sc_feat["desc"]
            # 施法属性：措辞为「…是你的施法属性（spellcasting ability）。你的 X（XXX）调整值决定…」；
            # 部分职业（德/圣/游）施法特性以表格直排，特性正文很短——改为全职业原文兜底搜索
            body_raw = "\n".join(body)
            am = re.search(
                r"施法属性（spellcasting ability）。\s*你的(力量|敏捷|体质|智力|感知|魅力)（(STR|DEX|CON|INT|WIS|CHA)）调整值", sc
            ) or re.search(
                r"施法属性（spellcasting ability）。\s*你的(力量|敏捷|体质|智力|感知|魅力)（(STR|DEX|CON|INT|WIS|CHA)）调整值", body_raw
            ) or re.search(
                r"施法属性[^。]{0,80}?(力量|敏捷|体质|智力|感知|魅力)（(STR|DEX|CON|INT|WIS|CHA)）", body_raw
            )
            if am:
                entry["spell_ability"] = ABILITY_ZH_ID.get(am.group(1)) or ABILITY_ABBR_ID.get(am.group(2))
            # 法术来源：只取引导句（首个 5 级子块之前），避免"秘闻/自选列表"句里的其他来源误入
            sc_intro = sc.split("#####")[0]
            for zh_src, sid in SOURCE_ZH_ID.items():
                if re.search(rf"{zh_src}（(?:Arcane|Divine|Primordial|Wyrd)", sc_intro):
                    if sid not in entry["sources"]:
                        entry["sources"].append(sid)
        # Improvement 可选天赋表
        imp = features.get("improvement")
        if imp:
            for zh_list, lid in (("魔法", "magic"), ("武艺", "martial"), ("技术", "technical")):
                if re.search(rf"{zh_list}(?:或(?:魔法|武艺|技术))?天赋", imp["desc"]):
                    if lid not in entry["improvement_talents"]:
                        entry["improvement_talents"].append(lid)
        # 进度表（表头驱动：全列捕获 → progression，并保留既有兼容字段）
        for start, rows in find_tables(body):
            header = [inner_zh(c) for c in rows[0]]
            N = len(header)
            if any("等级" in c for c in header):
                col_keys = [_class_table_key(c) for c in header]
                feat_idx = next((i for i, k in enumerate(col_keys) if k == "features"), None)
                columns = [{"key": k, "label": header[i].strip()} for i, k in enumerate(col_keys) if k]
                int_keys = {"cantrips", "rituals", "spells", "rages", "beast_forms", "augment_effects",
                            "augmented_items", "technique_points", "invocations", "sorcery_points", "prepared"}
                progression = {"columns": columns, "rows": {}}

                def cell_of(row, idx):
                    """行内取值：短行右侧补空；特性列内杂散 | 造成右移时，仅对特性列及其后的列做偏移校正。"""
                    if len(row) < N:
                        row = row + [""] * (N - len(row))
                    shift = len(row) - N
                    pos = idx + shift if (shift > 0 and feat_idx is not None and idx >= feat_idx) else idx
                    return row[pos] if 0 <= pos < len(row) else ""

                for row in rows[1:]:
                    lm = re.match(r"(\d+)\s*级", inner_zh(row[0]))
                    if not lm:
                        continue
                    level = int(lm.group(1))
                    values = {}
                    for idx, key in enumerate(col_keys):
                        if not key or key == "level":
                            continue
                        clean = inner_zh(cell_of(row, idx)).strip()
                        if key == "features":
                            feats = [re.sub(r"[_*`]", "", f).strip() for f in split_respect_parens(clean)]
                            values["features"] = [f for f in feats if f]
                        elif key.startswith("slot_") or key in int_keys:
                            num = re.match(r"(\d+)", clean)
                            values[key] = int(num.group(1)) if num else 0
                        else:
                            values[key] = clean
                    progression["rows"][str(level)] = values
                    # ---- 兼容字段：沿用旧的 table/slots/known 结构供既有代码消费 ----
                    entry["table"][str(level)] = {"features": values.get("features", [])}
                    slots = []
                    for ring in range(1, 10):
                        count = int(values.get(f"slot_{ring}", 0) or 0)
                        while len(slots) < ring:
                            slots.append(0)
                        slots[ring - 1] = count
                    while slots and slots[-1] == 0:
                        slots.pop()
                    entry["slots"][str(level)] = slots
                    for key, target in (("spells", "spells_known"), ("prepared", "prepared"),
                                        ("cantrips", "cantrips_known"), ("rituals", "rituals_known")):
                        if any(k == key for k in col_keys):
                            entry[target][str(level)] = int(values.get(key, 0) or 0)
                entry["progression"] = progression
                break
        # 子职业
        for sub_title, sub_body in split_sections(body, r"^#{2,4} .+（.+ Subclass）$"):
            sub_zh, sub_en = split_annot(sub_title.lstrip("# ").strip())
            if sub_en == f"{name_en} Subclass":
                continue
            sub_name = sub_en.replace(" Subclass", "").strip()
            sub_features, sub_order = class_features_from_body(sub_body)
            # 子职业导语（标题后、首个标题/表格/标记之前的一段文字）
            intro_lines = []
            for line in sub_body:
                s = line.strip()
                if not s:
                    continue
                if s.startswith("#") or s.startswith("|") or s.startswith("_"):
                    break
                intro_lines.append(s)
            entry["subclasses"][slugify(sub_name)] = {
                "name": sub_zh.replace("子职业", "").strip(),
                "name_en": sub_name,
                "desc": strip_annotations(" ".join(intro_lines)).strip(),
                "features": sub_features,
                "feature_order": sub_order,
            }
        # 标记法双向归位（兜底译文排布异常与同名 slug 冲突）：
        #  - 本职特性若标记含某个子职业名 → 移入该子职业（如机械师「秘法金属」误排在职区）
        #  - 子职业特性若标记不含其子职业名、且本职无同名项 → 移回本职
        # 名称匹配放宽：中文标记常省「领域」（生命领域 → 生命特性）
        def _name_candidates(name):
            name = (name or "").strip()
            out = {name}
            if "领域" in name:
                out.add(name.replace("领域", "").strip())
            return {x for x in out if x}

        sub_names = {sid: _name_candidates(sub.get("name")) for sid, sub in entry["subclasses"].items()}
        for slug, feat in list(entry["features"].items()):
            marker = str(feat.get("marker_zh") or "")
            for sid, names in sub_names.items():
                if any(sname in marker for sname in names):
                    entry["subclasses"][sid]["features"].setdefault(slug, feat)
                    entry["features"].pop(slug, None)
                    break
        for sid, sub in entry["subclasses"].items():
            names = _name_candidates(sub.get("name"))
            for slug, feat in list(sub["features"].items()):
                marker = str(feat.get("marker_zh") or "")
                if not any(sname in marker for sname in names) and slug not in entry["features"]:
                    entry["features"][slug] = feat
                    sub["features"].pop(slug, None)
        # 清理解析用临时字段（desc_raw），避免落盘
        for feature in entry["features"].values():
            feature.pop("desc_raw", None)
        for sub in entry["subclasses"].values():
            for feature in sub["features"].values():
                feature.pop("desc_raw", None)
        data[slugify(name_en)] = entry
    save("classes", data)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="all")
    args = parser.parse_args()
    only = args.only
    sections = {
        "glossary": build_glossary,
        "lineage": build_lineage,
        "talents": build_talents,
        "backgrounds": build_backgrounds,
        "chargen": build_chargen,
        "conditions": build_conditions,
        "spells": build_spells,
        "items": build_items,
        "monsters": build_monsters,
        "classes": build_classes,
        "gods": build_gods,
    }
    order = (["glossary", "lineage", "talents", "backgrounds", "chargen", "conditions",
              "spells", "items", "monsters", "classes", "gods"] if only == "all" else [only])
    for key in order:
        if key not in sections:
            print(f"未知分节：{key}")
            continue
        sections[key]()
    print("完成。")


if __name__ == "__main__":
    main()
