"""
法术系统：数据访问、建卡/升级校验、施法结算、专注与效果时长。

数据：
  - data/srd/spells.json      全量法术 0-9 环（构建脚本 scripts/build_bfrd.py，
                              数据：Black Flag SRD 中译稿（ORC 许可，见 v0.5.6 致谢页））
  - data/srd/spell_rules.json 法术位表 / 已知数量表 / 推荐包 / 施法职业表

分工：本模块只管"角色身上的法术状态"（已知列表、法术位、效果、专注），
所有随机数走 engine；房间广播与持久化由调用方（main/ai）负责。

建卡三模式（spec 6.3）：
  recommended 推荐包打底 / class 职业表内自选 / free 完全自由（跨职业表，房规）
数量上限按 spell_rules.json 的"职业 × 等级"表执行；自由模式只放开职业表，不放数量。
专注：受伤时由 state.adjust_hp 调用 concentration_save 自动检定；
效果带 rounds，由 state.next_turn 每轮 tick，到期自动播报。
"""

import re

from . import engine, srd

# 熟练加值表（1-20 级）：1-4 档 +2、5-8 档 +3、9-12 档 +4、13-16 档 +5、17-20 档 +6
PROF_BY_LEVEL = {
    1: 2, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 3, 8: 3,
    9: 4, 10: 4, 11: 4, 12: 4, 13: 5, 14: 5, 15: 5, 16: 5,
    17: 6, 18: 6, 19: 6, 20: 6,
}
# 属性提升（ASI）等级：到这些等级各获得一次提升（合计 +2，单项上限 20）
ASI_LEVELS = (4, 8, 12, 16, 19)
# 等级上限（v0.4.0 起支持 1-20 级；战役节奏公式不变，长战役自然升更高）
MAX_LEVEL = 20


# ---------------------------------------------------------------------------
# 数据访问
# ---------------------------------------------------------------------------


def spell(spell_id):
    """按 id 取法术条目；找不到抛中文 ValueError。"""
    entry = srd.spells().get(str(spell_id))
    if not entry:
        raise ValueError(f"未知法术：{spell_id}")
    return entry


def find_spell(query):
    """按 id / 中文名 / 英文名匹配（含包含匹配），返回 (id, 条目) 或 None。"""
    key = str(query or "").strip().lower()
    if not key:
        return None
    data = srd.spells()
    for sid, entry in data.items():
        if sid == key or entry.get("name", "").lower() == key or entry.get("name_en", "").lower() == key:
            return sid, entry
    for sid, entry in data.items():
        if key in sid or key in entry.get("name", "") or key in entry.get("name_en", "").lower():
            return sid, entry
    return None


def is_caster(klass_id):
    return klass_id in (srd.spell_rules().get("caster_classes") or [])


# ---------------------------------------------------------------------------
# 数量与法术位
# ---------------------------------------------------------------------------


def slots_for(klass_id, char_level):
    """返回该职业在该等级的法术位数组：[1 环数量, 2 环数量, ...]（不足补到实有环数）。"""
    table = (srd.spell_rules().get("slots") or {}).get(klass_id) or {}
    row = table.get(str(max(1, min(MAX_LEVEL, int(char_level)))))
    return list(row or [])


def limits_for(klass_id, char_level, character=None):
    """
    返回该职业在该等级的法术容量：
      cantrips 戏法上限 / known 已知法术上限 / prepared 准备法术上限 / rituals 仪式上限 /
      spells（兼容旧调用：已知型=known，准备型=prepared）/ max_spell_level 最高可施法环阶。
    BFRD 模型：
      - 已知型（吟游/术士/邪术师/圣武士/游侠）用职业表「已知法术」列；
      - 准备型（牧师/德鲁伊/法师）用公式（调整值 + 等级；法师=智力+等级）；
      - 仪式列见法师/牧师/德鲁伊/吟游（独立于以上，不计入法术数量）。
    """
    counts = (srd.spell_rules().get("counts") or {}).get(klass_id)
    if not counts:
        raise ValueError(f"职业 {klass_id} 不是施法职业")
    index = max(0, min(MAX_LEVEL, int(char_level)) - 1)
    slots = slots_for(klass_id, char_level)
    max_spell_level = max((i + 1 for i, n in enumerate(slots) if n > 0), default=0)
    klass = srd.klass(klass_id)

    def column(name):
        table = klass.get(name) or {}
        value = table.get(str(int(char_level)))
        return int(value) if value is not None else 0

    cantrips = column("cantrips_known")
    known = column("spells_known")
    rituals = column("rituals_known")
    rule = counts.get("prepared_rule")
    prepared = 0
    if rule:
        if character is not None:
            ability_mod = int((character.get("mods") or {}).get(rule.get("ability"), 0))
            prepared = max(1, int(ability_mod + float(rule.get("per_level", 1)) * int(char_level)))
        else:
            prepared = 2 if int(char_level) == 1 else 1
    spells_cfg = counts.get("spells")
    if spells_cfg:
        legacy_spells = spells_cfg[index]
    else:
        legacy_spells = prepared
    return {
        "cantrips": cantrips,
        "spells": legacy_spells,
        "known": known,
        "prepared": prepared,
        "rituals": rituals,
        "max_spell_level": max_spell_level,
    }


# ---------------------------------------------------------------------------
# 法术三分层（BFRD）：戏法 / 已知（已知型职业）或准备（准备型职业） / 法术书（法师）/ 仪式
# ---------------------------------------------------------------------------

KNOWN_TRACK_CLASSES = ("bard", "sorcerer", "warlock", "paladin", "ranger")
PREPARED_TRACK_CLASSES = ("cleric", "druid", "wizard")
BUCKET_LABELS = {"cantrips": "戏法", "known": "已知法术", "prepared": "准备法术",
                 "spellbook": "法术书", "rituals": "仪式"}


def caster_track(klass_id):
    """该职业的施法结构：known/prepared/spellbook/rituals 四种能力（None=非法师职业）。"""
    if not is_caster(klass_id):
        return None
    klass = srd.klass(klass_id)
    return {
        "known": klass_id in KNOWN_TRACK_CLASSES,
        "prepared": klass_id in PREPARED_TRACK_CLASSES,
        "spellbook": klass_id == "wizard",
        "rituals": bool(klass.get("rituals_known")),
        "cantrips": bool(klass.get("cantrips_known")),
    }


def spell_model(character):
    """法术数据结构版本：1=旧（单一已知列表）；2=分层（戏法/已知或准备/法术书/仪式）。"""
    return int(character.get("spell_model") or 1)


def bucket_entries(character, bucket):
    return list(character.get(bucket) or [])


def all_bucket_entries(character):
    out = []
    for bucket in ("cantrips", "known", "prepared", "spellbook", "rituals"):
        out.extend(bucket_entries(character, bucket))
    return out


def has_spell(character, spell_id):
    """该角色是否在任何栏位已有此法术（分层模型）或旧列表里（旧模型）。"""
    if spell_model(character) >= 2:
        return any(e.get("id") == spell_id for e in all_bucket_entries(character))
    return any(s.get("id") == spell_id for s in character.get("spells") or [])


def rebuild_spell_union(character):
    """维护兼容字段 spells（= 各栏位并集，去重）供展示、简报与旧调用方使用。"""
    if spell_model(character) < 2:
        return
    union, seen = [], set()
    for entry in all_bucket_entries(character):
        sid = entry.get("id")
        if sid and sid not in seen:
            union.append(dict(entry))
            seen.add(sid)
    character["spells"] = union


def castable_ids(character, ritual_mode=False):
    """当前可施放的法术 id 集合（cast_spell 的唯一校验来源）。"""
    if spell_model(character) < 2:
        return {s.get("id") for s in character.get("spells") or []}
    if ritual_mode:
        return {e.get("id") for e in bucket_entries(character, "rituals")}
    level_zero = {e.get("id") for e in bucket_entries(character, "cantrips")}
    if character.get("class_id") == "wizard":
        leveled = bucket_entries(character, "prepared")
    elif character.get("class_id") in KNOWN_TRACK_CLASSES:
        leveled = bucket_entries(character, "known")
    else:
        leveled = bucket_entries(character, "prepared")
    return level_zero | {e.get("id") for e in leveled}


def _source_ok(character, entry):
    """法术来源校验（'完全自由'模式跳过），返回 (ok, 原因)。"""
    if character.get("spell_mode") == "free":
        return True, ""
    klass = {}
    try:
        klass = srd.klass(character.get("class_id"))
    except ValueError:
        klass = {}
    class_sources = set(klass.get("sources") or [])
    spell_sources = set(entry.get("sources") or [])
    if class_sources and spell_sources and not (class_sources & spell_sources):
        return False, "不在其法术来源列表内；完全自由模式可跨列表"
    if not spell_sources and character.get("class_id") not in (entry.get("classes") or []):
        return False, "职业表内没有该法术；完全自由模式可跨职业表"
    return True, ""


def learn_spell(character, spell_id, entry, bucket=None, free=True):
    """
    习得/添加法术到指定栏位（分层模型）。bucket 省略时按职业与环阶推断。
    法师法术书：free=True 时消耗每级 2 条的免费名额；卷轴抄录请传 free=False（GM 裁定）。
    """
    if spell_model(character) < 2:
        return _learn_spell_legacy(character, spell_id, entry)
    klass_id = character.get("class_id") or ""
    track = caster_track(klass_id)
    if not track:
        raise ValueError("该职业不能学习法术")
    level = int(entry.get("level", 0))
    if bucket in (None, "", "auto"):
        if level == 0 and track["cantrips"]:
            bucket = "cantrips"
        elif track["spellbook"]:
            bucket = "spellbook"
        elif track["known"]:
            bucket = "known"
        else:
            bucket = "prepared"
    if bucket not in BUCKET_LABELS:
        raise ValueError("未知法术栏位")
    if bucket == "cantrips" and not track["cantrips"]:
        raise ValueError("该职业没有戏法栏位")
    if bucket == "known" and not track["known"]:
        raise ValueError("该职业不使用「已知法术」栏位")
    if bucket == "prepared" and not track["prepared"]:
        raise ValueError("该职业不使用「准备法术」栏位")
    if bucket == "spellbook" and not track["spellbook"]:
        raise ValueError("只有法师使用法术书")
    if bucket == "rituals" and not track["rituals"]:
        raise ValueError("该职业没有仪式栏位")
    if bucket == "cantrips" and level != 0:
        raise ValueError("戏法栏位只能放 0 环法术")
    if bucket != "cantrips" and level == 0:
        raise ValueError("戏法请放入戏法栏位")
    if bucket == "rituals" and not entry.get("ritual"):
        raise ValueError(f"「{entry.get('name', '')}」不是仪式法术")
    if bucket == "prepared":
        # 准备栏是法术书/职业列表的子集：只防"重复准备"，不参与跨栏查重
        if any(e.get("id") == spell_id for e in bucket_entries(character, "prepared")):
            raise ValueError(f"「{entry.get('name', '')}」已经准备")
    elif has_spell(character, spell_id):
        raise ValueError(f"「{entry.get('name', '')}」已经学会（任一栏位内）")
    ok, why = _source_ok(character, entry)
    if not ok:
        raise ValueError(f"{character.get('name', '')} 无法学习「{entry.get('name', '')}」（{why}）")
    limits = limits_for(klass_id, character.get("level", 1), character)
    if level > limits["max_spell_level"]:
        raise ValueError(f"当前等级还不能施放 {level} 环阶法术（最高 {limits['max_spell_level']} 环阶）")
    note = ""
    if bucket == "cantrips":
        if len(bucket_entries(character, "cantrips")) >= limits["cantrips"]:
            raise ValueError(f"戏法数量已达上限（{limits['cantrips']} 个），请先移除一个")
    elif bucket == "known":
        if len(bucket_entries(character, "known")) >= limits["known"]:
            raise ValueError(f"已知法术数量已达上限（{limits['known']} 个），请先替换或遗忘")
    elif bucket == "prepared":
        if len(bucket_entries(character, "prepared")) >= limits["prepared"]:
            raise ValueError(f"准备法术数量已达上限（{limits['prepared']} 个），请先取消其他准备")
        if track["spellbook"] and not any(e.get("id") == spell_id for e in bucket_entries(character, "spellbook")):
            raise ValueError("法师只能准备法术书中的法术（请先加入法术书）")
    elif bucket == "rituals":
        if len(bucket_entries(character, "rituals")) >= limits["rituals"]:
            raise ValueError(f"仪式数量已达上限（{limits['rituals']} 个）")
    elif bucket == "spellbook" and free:
        credits = int(character.get("spellbook_credits") or 0)
        if credits <= 0:
            raise ValueError("法术书免费名额已用完（每级 +2；卷轴抄录请由 GM 以抄录方式加入）")
        character["spellbook_credits"] = credits - 1
        note = f"（剩余免费名额 {credits - 1}）"
    character.setdefault(bucket, []).append(
        {"id": spell_id, "name": entry.get("name", spell_id), "level": level}
    )
    rebuild_spell_union(character)
    return f"{character.get('name', '')} 学会「{entry.get('name', '')}」（{BUCKET_LABELS[bucket]}）{note}"


def _learn_spell_legacy(character, spell_id, entry):
    """旧模型（单一列表）的习得逻辑：保留供未迁移角色使用。"""
    limits = limits_for(character.get("class_id"), character.get("level", 1), character)
    mode = character.get("spell_mode", "class")
    known = list(character.get("spells") or [])
    if any(s.get("id") == spell_id for s in known):
        raise ValueError(f"「{entry['name']}」已经学会")
    if mode != "free":
        ok, why = _source_ok(character, entry)
        if not ok:
            raise ValueError(f"{character.get('name', '')} 无法学习「{entry['name']}」（{why}）")
    level = int(entry.get("level", 0))
    if level > limits["max_spell_level"]:
        raise ValueError(f"当前等级还不能施放 {level} 环阶法术（最高 {limits['max_spell_level']} 环阶）")
    same_level = [s for s in known if int(s.get("level", 0)) == level]
    cap = limits["cantrips"] if level == 0 else limits["spells"]
    if len(same_level) >= cap:
        kind = "戏法" if level == 0 else "法术"
        raise ValueError(f"{kind}数量已达上限（{cap} 个），请先遗忘一个（forget_spell）")
    known.append({"id": spell_id, "name": entry["name"], "level": level})
    character["spells"] = known
    kind = "戏法" if level == 0 else f"{level} 环阶法术"
    return f"{character.get('name', '')} 学会「{entry['name']}」（{kind}）"


def forget_spell(character, query, bucket=None):
    """遗忘/移除一个法术（腾出栏位名额）。bucket 省略时自动查找。"""
    key = str(query or "").strip()
    if spell_model(character) < 2:
        for entry in list(character.get("spells") or []):
            if key and (key == entry.get("id") or key == entry.get("name") or key in entry.get("name", "")):
                character["spells"].remove(entry)
                return f"{character.get('name', '')} 忘却「{entry.get('name', '')}」"
        raise ValueError(f"没有找到法术：{query}")
    order = [bucket] if bucket else ["known", "prepared", "rituals", "cantrips", "spellbook"]
    for name in order:
        for entry in list(character.get(name) or []):
            if key and (key == entry.get("id") or key == entry.get("name") or key in entry.get("name", "")):
                character[name].remove(entry)
                rebuild_spell_union(character)
                return f"{character.get('name', '')} 忘却「{entry.get('name', '')}」（{BUCKET_LABELS.get(name, name)}）"
    raise ValueError(f"没有找到法术：{query}")


def swap_spell(character, old_query, new_id, new_entry):
    """
    已知型职业升级替换（BFRD：吟游/术士/邪术师/圣武士/游侠 每级可替换 1 条已知法术）。
    消耗 swap_credits；新法术须满足来源、环阶与去重校验。
    """
    if spell_model(character) < 2:
        raise ValueError("该角色使用旧版法术列表（请先由 GM 迁移或重建）")
    if int(character.get("swap_credits") or 0) <= 0:
        raise ValueError("没有可用的替换名额（升级时获得 1 个）")
    known = character.setdefault("known", [])
    target = None
    key = str(old_query or "").strip()
    for entry in known:
        if key and (key == entry.get("id") or key == entry.get("name") or key in entry.get("name", "")):
            target = entry
            break
    if not target:
        raise ValueError(f"已知法术里没有找到：{old_query}")
    if has_spell(character, new_id):
        raise ValueError(f"「{new_entry.get('name', '')}」已经在你的法术里了")
    ok, why = _source_ok(character, new_entry)
    if not ok:
        raise ValueError(f"无法替换为「{new_entry.get('name', '')}」（{why}）")
    level = int(new_entry.get("level", 0))
    limits = limits_for(character.get("class_id"), character.get("level", 1), character)
    if level < 1 or level > limits["max_spell_level"]:
        raise ValueError(f"替换的法术必须是 1 到 {limits['max_spell_level']} 环（当前角色等级）")
    known.remove(target)
    known.append({"id": new_id, "name": new_entry.get("name", new_id), "level": level})
    character["swap_credits"] = int(character.get("swap_credits") or 0) - 1
    rebuild_spell_union(character)
    return (
        f"{character.get('name', '')} 替换法术：「{target.get('name')}」 → 「{new_entry.get('name')}」"
        f"（剩余替换名额 {character['swap_credits']}）"
    )


def recommended_picks(klass_id, char_level, mode="recommended"):
    """推荐包：按上限截取（戏法先取满，再取法术）。返回 id 列表。"""
    rules = srd.spell_rules()
    picks = rules.get("recommended", {}).get(klass_id) or []
    if mode == "free":
        return list(picks)
    limits = limits_for(klass_id, char_level)
    data = srd.spells()
    cantrips, leveled = [], []
    for sid in picks:
        entry = data.get(sid)
        if not entry:
            continue
        (cantrips if entry["level"] == 0 else leveled).append(sid)
    return cantrips[: limits["cantrips"]] + leveled[: limits["spells"]]


def validate_spell_picks(picks, klass_id, char_level, mode="class"):
    """建卡/编辑时的法术选择校验，返回中文错误列表（空 = 通过）。"""
    errors = []
    picks = [str(p) for p in (picks or [])]
    if len(picks) != len(set(picks)):
        errors.append("法术不能重复选择")
    limits = limits_for(klass_id, char_level)    # 非施法职业在此抛错
    data = srd.spells()
    cantrip_count, leveled_count = 0, 0
    for sid in picks:
        entry = data.get(sid)
        if not entry:
            errors.append(f"未知法术：{sid}")
            continue
        if mode != "free" and klass_id not in (entry.get("classes") or []):
            errors.append(f"该职业无法学习「{entry['name']}」")
        if entry["level"] == 0:
            cantrip_count += 1
        else:
            leveled_count += 1
            if entry["level"] > limits["max_spell_level"]:
                errors.append(f"「{entry['name']}」超出当前可施法环阶（最高 {limits['max_spell_level']} 环）")
    if cantrip_count > limits["cantrips"]:
        errors.append(f"戏法最多 {limits['cantrips']} 个（已选 {cantrip_count}）")
    if leveled_count > limits["spells"]:
        errors.append(f"法术最多 {limits['spells']} 个（已选 {leveled_count}）")
    return errors


def build_spell_state(picks, klass_id, char_level, mode):
    """建卡用：生成角色身上的法术字段（已知列表、法术位、空效果/专注）。"""
    data = srd.spells()
    spells = []
    for sid in picks:
        entry = data.get(sid)
        if not entry:
            continue
        spells.append({"id": sid, "name": entry["name"], "level": entry["level"]})
    slots_cfg = slots_for(klass_id, char_level)
    slots = {str(i + 1): {"max": n, "used": 0} for i, n in enumerate(slots_cfg) if n > 0}
    return {
        "spell_mode": mode,
        "spells": spells,
        "spell_slots": slots,
        "effects": [],
        "concentration": None,
    }


# ---------------------------------------------------------------------------
# 伤害/治疗掷骰
# ---------------------------------------------------------------------------


def damage_dice(block, char_level, slot_level, base_level):
    """按施法环阶（逐环表）或角色等级（戏法表）取伤害骰表达式。"""
    if not block:
        return None
    if block.get("by_slot"):
        table = block["by_slot"]
        if str(slot_level) in table:
            return table[str(slot_level)]
        best = None
        for key, dice in table.items():
            if str(key).isdigit() and int(key) <= int(slot_level):
                if best is None or int(key) > int(best[0]):
                    best = (key, dice)
        if best:
            return best[1]
        return next(iter(table.values()))
    if block.get("by_char_level"):
        table = block["by_char_level"]
        best = None
        for key, dice in table.items():
            if str(key).isdigit() and int(key) <= int(char_level):
                if best is None or int(key) > int(best[0]):
                    best = (key, dice)
        if best:
            return best[1]
        return next(iter(table.values()))
    return None


def _ability_modifier(character, ability):
    return int((character.get("mods") or {}).get(ability, 0))


def _caster_ability(character):
    klass_id = character.get("class_id")
    if not klass_id:
        return None
    try:
        return srd.klass(klass_id).get("spell_ability")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 效果与专注
# ---------------------------------------------------------------------------


def format_minutes(minutes):
    """把分钟数格式化成中文时长：0.3 → '3 轮'；90 → '1 小时 30 分钟'。"""
    minutes = float(minutes or 0)
    if minutes < 1:
        return f"{max(1, int(round(minutes * 10)))} 轮"
    whole = int(round(minutes))
    if whole < 60:
        return f"{whole} 分钟"
    hours, mins = divmod(whole, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {mins} 分钟" if mins else "")
    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


def effect_status(effect, clock_minutes):
    """面板/简报用的效果状态文本：'剩 8 分钟' 或 '持续'。"""
    expires = effect.get("expires_at")
    if expires is None:
        return "持续"
    remaining = max(0.0, float(expires) - float(clock_minutes or 0.0))
    return "剩 " + format_minutes(remaining)


def add_effect(character, name, minutes, clock_minutes=0.0, concentration=False, note=""):
    """
    给角色挂一个效果。minutes > 0 时按"游戏时钟"记绝对到期时间（expires_at），
    minutes <= 0 表示持续到手动移除。专注类会替换旧的专注效果。
    注意时间刻度：1 分钟 = 10 轮，1 轮 = 0.1 分钟（6 秒）。
    """
    character.setdefault("effects", [])
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("效果需要名字")
    try:
        minutes = max(0.0, float(minutes))
    except (TypeError, ValueError):
        minutes = 0.0
    try:
        clock_minutes = float(clock_minutes or 0.0)
    except (TypeError, ValueError):
        clock_minutes = 0.0
    replaced = None
    if concentration:
        for effect in list(character.get("effects") or []):
            if effect.get("concentration"):
                character["effects"].remove(effect)
                replaced = effect.get("name")
        character["concentration"] = name
    expires_at = round(clock_minutes + minutes, 2) if minutes > 0 else None
    keep_old_text = None
    if not concentration:
        # 组合魔法（BFRD）：同一效果不叠加——同名非专注效果只保留"更强"者
        # （无限持续 > 有限持续；有限之间比较到期时间，较晚者强）
        for effect in list(character.get("effects") or []):
            if effect.get("name") != name or effect.get("concentration"):
                continue
            old_exp = effect.get("expires_at")
            old_stronger = old_exp is None or (expires_at is not None and old_exp >= expires_at)
            if old_stronger:
                keep_old_text = f"效果「{name}」已在身上，同一效果不叠加（保留较强者，未重复施加）"
            else:
                character["effects"].remove(effect)
            break
    if keep_old_text:
        return keep_old_text
    character["effects"].append(
        {
            "name": name,
            "expires_at": expires_at,
            "concentration": bool(concentration),
            "note": str(note or "")[:200],
        }
    )
    text = f"效果「{name}」已施加"
    if minutes > 0:
        text += f"（{format_minutes(minutes)}）"
    else:
        text += "（持续到解除）"
    if replaced:
        text += f"；原专注效果「{replaced}」结束"
    return text


def remove_effect(character, query):
    """按名字（或名字包含）移除效果。"""
    key = str(query or "").strip()
    for effect in list(character.get("effects") or []):
        if key and (key == effect.get("name") or key in effect.get("name", "")):
            character["effects"].remove(effect)
            if effect.get("concentration"):
                character["concentration"] = None
            return f"效果「{effect['name']}」已移除"
    raise ValueError(f"没有找到效果：{query}")


def expire_by_clock(character, clock_minutes):
    """
    按游戏时钟结算：移除所有已到期的效果（专注到期同步清空 concentration），
    返回到期播报文本列表。由战斗回合推进与 advance_time/休息调用。
    """
    expired = []
    for effect in list(character.get("effects") or []):
        expires = effect.get("expires_at")
        if expires is not None and float(clock_minutes) >= float(expires):
            character["effects"].remove(effect)
            if effect.get("concentration"):
                character["concentration"] = None
            expired.append(f"{character.get('name', '')} 的效果「{effect['name']}」已消退")
    return expired


def concentration_save(character, damage, rng=None):
    """
    受伤时的专注检定（DC = max(10, 伤害一半)）。
    失败则中断专注效果。返回播报文本；未在专注或零伤害返回 None。
    rng 参数供单元测试注入固定随机源。
    """
    if not character.get("concentration") or damage <= 0:
        return None
    dc = max(10, damage // 2)
    bonus = int((character.get("saves") or {}).get("con", 0))
    result = engine.roll_check(modifier=bonus, dc=dc, label="专注检定", rng=rng)
    name = character["concentration"]
    if result["success"]:
        return f"专注检定（DC {dc}）：{result['breakdown']} → 保持「{name}」"
    for effect in list(character.get("effects") or []):
        if effect.get("concentration"):
            character["effects"].remove(effect)
    character["concentration"] = None
    return f"专注检定（DC {dc}）：{result['breakdown']} → 失败，「{name}」中断"


# ---------------------------------------------------------------------------
# 施法
# ---------------------------------------------------------------------------


def cast_spell(character, spell_id, slot_level=None, clock_minutes=0.0, as_ritual=False):
    """
    施法结算（引擎权威）：按法术 id 校验已知/环阶/法术位 → 扣位 → 掷伤害或治疗 →
    处理专注与效果（效果按游戏时钟记到期时间）。

    as_ritual=True 时为仪式施法（BFRD）：法术必须带仪式旗标；不消耗法术位、
    不可升环，但效果按"法术所列环阶与最高可用法术位中较高者"折算。
    穿甲施法校验与力竭提示以文字形式附加在结果中。
    伤害/治疗只掷骰不自动落到目标——由 AI 再调 adjust_hp 结算到具体目标。
    """
    from . import characters as characters_mod    # 延迟导入避免循环
    entry = spell(spell_id)    # 未知 id 直接抛 ValueError
    if character.get("dead"):
        raise ValueError(f"{character.get('name', '')} 已死亡，普通手段无法施法（需要复苏术/复活术等）")
    if character.get("dying") or character.get("stable"):
        raise ValueError(f"{character.get('name', '')} 昏迷中，无法施法")
    # 可施放校验（分层模型）：戏法/已知 或 准备；仪式施法走仪式栏位
    if as_ritual:
        if spell_id not in castable_ids(character, ritual_mode=True):
            raise ValueError(f"{character.get('name', '')} 未掌握该仪式（仪式栏位中没有「{entry['name']}」）")
    elif spell_id not in castable_ids(character, ritual_mode=False):
        if spell_model(character) >= 2:
            where = "准备列表" if character.get("class_id") in PREPARED_TRACK_CLASSES else "已知列表"
            raise ValueError(f"{character.get('name', '')} 未习得该法术（「{entry['name']}」不在{where}中）")
        raise ValueError(f"{character.get('name', '')} 未习得该法术（{entry['name']}）")
    level = int(entry.get("level", 0))
    slots = character.setdefault("spell_slots", {})
    used_slot = None
    parts = [f"{character.get('name', '')} 施放「{entry['name']}」"]
    if as_ritual:
        if not entry.get("ritual"):
            raise ValueError(f"「{entry['name']}」不是仪式法术")
        parts.append("仪式施法（不消耗法术位；需约 10 分钟，由引擎推进时钟）")
    elif level > 0:
        if slot_level is None:
            slot_level = level
        try:
            slot_level = int(slot_level)
        except (TypeError, ValueError):
            raise ValueError("施法环阶无效")
        if slot_level < level:
            raise ValueError(f"「{entry['name']}」至少需要 {level} 环法术位")
        slot = slots.get(str(slot_level))
        if not slot:
            raise ValueError(f"没有 {slot_level} 环法术位")
        if slot.get("used", 0) >= slot.get("max", 0):
            raise ValueError(f"{slot_level} 环法术位已用尽")
        slot["used"] = slot.get("used", 0) + 1
        used_slot = slot_level
        parts.append(f"消耗 {slot_level} 环法术位（{slot['used']}/{slot['max']}）")
    else:
        parts.append("戏法（不消耗法术位）")
    meta = {"spell": spell_id, "name": entry["name"], "level": level, "slot_level": used_slot, "rolls": [], "casting_bonus": 0, "ritual": bool(as_ritual)}
    # 学派专精：「所选学派的法术攻击加值与豁免 DC +1」（施法时按法术学派生效，叙事侧采用）
    schools = character.get("talent_schools") or {}
    if schools.get("school-specialization") and entry.get("school") == schools["school-specialization"]:
        meta["casting_bonus"] = 1
        parts.append(f"学派专精（{schools['school-specialization']}）：本法术攻击加值与豁免 DC +1")
    # 穿甲施法校验（文字提醒）与力竭提示
    armor_warning = characters_mod.armor_proficiency_warning(character)
    if armor_warning:
        parts.append(armor_warning)
    exhaustion = int(character.get("exhaustion") or 0)
    if exhaustion >= 3:
        parts.append(f"（力竭 {exhaustion} 级：攻击检定与豁免具有劣势）")
    block = entry.get("damage") or {}
    # 伤害掷骰：仪式法术按"法术环阶与最高可用法术位中较高者"折算
    scale_circle = used_slot or level
    if as_ritual:
        best = 0
        for key, slot in (character.get("spell_slots") or {}).items():
            try:
                if int(slot.get("max", 0)) > 0:
                    best = max(best, int(key))
            except (TypeError, ValueError):
                continue
        scale_circle = max(level, best)
    dice = damage_dice(block, int(character.get("level", 1)), scale_circle, level)
    if dice:
        try:
            roll = engine.roll_expr(dice)
            meta["rolls"].append(
                {"kind": "damage", "dice": dice, "total": roll["total"], "breakdown": roll["breakdown"]}
            )
            line = f"伤害掷骰 {dice} = {roll['total']}"
            if block.get("type"):
                line += f"（{block['type']}）"
            if block.get("save"):
                line += f"；目标需 {block['save']} 豁免" + ("，成功减半" if block.get("save_success") == "half" else "")
            if block.get("attack"):
                line += f"；{block['attack']}"
            if block.get("note"):
                line += f"；{block['note']}"
            if entry.get("damage_note"):
                line += f"；{entry['damage_note']}"
            parts.append(line)
        except ValueError:
            parts.append(f"伤害骰「{dice}」无法解析，请人工结算")
    # 治疗掷骰（把"施法属性调整值"替换成具体数字后掷）
    if block.get("heal"):
        heal_expr = block["heal"]
        ability = _caster_ability(character)
        modifier = _ability_modifier(character, ability) if ability else 0
        heal_expr = re.sub(r"your spellcasting ability modifier", f"{modifier:+d}", heal_expr, flags=re.I)
        try:
            roll = engine.roll_expr(heal_expr)
            meta["rolls"].append(
                {"kind": "heal", "dice": heal_expr, "total": roll["total"], "breakdown": roll["breakdown"]}
            )
            parts.append(f"治疗掷骰 {heal_expr} = {roll['total']}（请用 adjust_hp 结算到目标）")
        except ValueError:
            parts.append(f"治疗量：{heal_expr}（无法解析，请人工结算）")
    # 效果与专注（时长换算成分钟：1 轮 = 0.1 分钟）
    if entry.get("concentration") or int(entry.get("duration_rounds") or 0) > 0:
        minutes = int(entry.get("duration_rounds") or 0) / 10.0
        if minutes <= 0 and entry.get("concentration"):
            minutes = 1.0    # 专注类通常有明确时长；兜底按 1 分钟
        parts.append(
            add_effect(
                character,
                entry["name"],
                minutes,
                clock_minutes=clock_minutes,
                concentration=bool(entry.get("concentration")),
                note=entry.get("duration") or "",
            )
        )
    return "；".join(parts), meta


# ---------------------------------------------------------------------------
# 学习 / 遗忘 / 升级
# ---------------------------------------------------------------------------


def level_up(character):
    """
    里程碑升级（1→20 级）：HP 取生命骰平均+1+体质、熟练加值随等级、
    技能/豁免/被动/施法数值重算、法术位按新等级重建（含 4-9 环）。
    升到 4/8/12/16/19 级记一次 pending_asi（BFRD 的 Improvement，由角色卡/GM/AI 分配）；
    3 级记 pending_subclass、10/20 级记 pending_boons（选项由 characters 从职业特性解析）。
    同伴等无玩家角色由调用方用 characters.settle_companion_asi 自动结算。
    返回中文摘要。
    """
    from . import characters as characters_mod    # 延迟导入：characters 在模块级依赖本模块
    level = int(character.get("level", 1))
    if level >= MAX_LEVEL:
        raise ValueError(f"等级上限为 {MAX_LEVEL} 级")
    old_level = level
    level += 1
    character["level"] = level
    # 升级变化清单所需：升级前的容量快照
    old_caps = None
    if character.get("class_id") and is_caster(character.get("class_id")):
        try:
            old_caps = limits_for(character["class_id"], old_level, character)
        except ValueError:
            old_caps = None
    class_improvements = characters_mod.improvement_levels(character.get("class_id") or "")
    if level in class_improvements:
        character["pending_asi"] = int(character.get("pending_asi", 0)) + 1
    prof = PROF_BY_LEVEL.get(level, 2 + (level - 1) // 4)
    character["prof_bonus"] = prof
    mods = character.get("mods") or {}
    # HP：每级取生命骰平均值（骰面//2+1）+ 体质修正 + 数值天赋（战斗锤炼每级 +2）
    con_mod = int(mods.get("con", 0))
    numbers = characters_mod.talent_numbers(character)
    gain = max(1, int(character.get("hit_die", 8)) // 2 + 1 + con_mod) + int(numbers["hp_per_level"])
    character["hp"]["max"] += gain
    character["hp"]["cur"] = min(character["hp"]["max"], character["hp"]["cur"] + gain)
    # 豁免/技能/被动重算（熟练加值可能变了）
    klass = {}
    if character.get("class_id"):
        try:
            klass = srd.klass(character["class_id"])
        except ValueError:
            klass = {}
    # BFRD：3 级选子职业；10/20 级获恩赐（选项来自职业特性文本，由角色卡/GM/AI 选择）
    if level == characters_mod.SUBCLASS_LEVEL and klass.get("subclasses") and not character.get("subclass"):
        character["pending_subclass"] = True
    if level in characters_mod.BOON_LEVELS:
        pending = [int(x) for x in (character.get("pending_boons") or [])]
        if level not in pending:
            pending.append(level)
        character["pending_boons"] = pending
    character["saves"] = {a: mods.get(a, 0) + (prof if a in klass.get("saves", []) else 0) for a in mods}
    all_skills = srd.skills()
    character["skills"] = {
        sid: mods.get(all_skills[sid]["ability"], 0) + prof
        for sid in (character.get("skills") or {})
        if sid in all_skills
    }
    character["passive_perception"] = (
        10 + int(mods.get("wis", 0)) + (prof if "perception" in character["skills"] else 0)
        + int(numbers["passive_bonus"])
    )
    if character.get("spellcasting"):
        ability = character["spellcasting"]["ability"]
        character["spellcasting"] = {
            "ability": ability,
            "save_dc": 8 + prof + int(mods.get(ability, 0)),
            "attack_bonus": prof + int(mods.get(ability, 0)),
        }
    # 施法结构：法师法术书免费名额 +2/级；已知型职业替换名额 +1/级（BFRD）
    track = None
    if character.get("class_id") and is_caster(character.get("class_id")):
        track = caster_track(character["class_id"])
        character.setdefault("spellbook_credits", 0)
        character.setdefault("swap_credits", 0)
        if track["spellbook"]:
            character["spellbook_credits"] = int(character["spellbook_credits"]) + 2
        if track["known"]:
            character["swap_credits"] = int(character["swap_credits"]) + 1
    # 法术位重建（保留已用数，不超过新上限）
    if klass.get("spell_ability"):
        slots_cfg = slots_for(character.get("class_id"), level)
        old_slots = character.get("spell_slots") or {}
        new_slots = {}
        for index, count in enumerate(slots_cfg):
            if count <= 0:
                continue
            key = str(index + 1)
            old_used = int((old_slots.get(key) or {}).get("used", 0))
            new_slots[key] = {"max": count, "used": min(old_used, count)}
        character["spell_slots"] = new_slots
    # ---- 升级变化清单（A：文档规定该变什么，这里逐项报出）----
    changes = [f"HP +{gain}"]
    changes.append(f"生命骰 +1（d{int(character.get('hit_die', 8))}，短休可消耗回血；长休恢复一半）")
    if PROF_BY_LEVEL.get(old_level) != prof:
        changes.append(f"熟练加值 {prof:+d}")
    if klass.get("spell_ability"):
        new_slot_row = slots_for(character.get("class_id"), level)
        old_slot_row = slots_for(character.get("class_id"), old_level)
        slot_diffs = []
        for ring in range(max(len(old_slot_row), len(new_slot_row))):
            a = old_slot_row[ring] if ring < len(old_slot_row) else 0
            b = new_slot_row[ring] if ring < len(new_slot_row) else 0
            if a != b:
                slot_diffs.append(f"{ring + 1}环 {a}→{b}")
        if slot_diffs:
            changes.append("法术位 " + "、".join(slot_diffs))
    limits = None
    if klass.get("spell_ability"):
        limits = limits_for(character.get("class_id"), level, character)
        if old_caps:
            cap_diffs = []
            for key, label in (("cantrips", "戏法"), ("known", "已知法术"),
                               ("prepared", "准备法术"), ("rituals", "仪式")):
                a, b = int(old_caps.get(key, 0)), int(limits.get(key, 0))
                if a != b:
                    cap_diffs.append(f"{label} {a}→{b}")
            if cap_diffs:
                changes.append("法术容量 " + "、".join(cap_diffs))
            changes.append(f"最高施法环阶 {limits['max_spell_level']} 环")
    row = ((klass.get("progression") or {}).get("rows") or {}).get(str(level))
    if row and row.get("features"):
        changes.append("新特性 " + "、".join(row["features"]))
    if track and track["spellbook"]:
        changes.append(f"法术书免费名额 +2（剩余 {character['spellbook_credits']}）")
    if track and track["known"]:
        changes.append(f"可替换 1 条已知法术（替换名额 {character['swap_credits']}）")
    text = f"{character.get('name', '')} 升到 {level} 级：" + "；".join(changes)
    if level in class_improvements:
        text += "；获得属性提升（待在角色卡分配：+2 一项 / +1 两项 / +1 一项加天赋）"
    else:
        next_asi = next((lv for lv in class_improvements if lv > level), None)
        if next_asi:
            text += f"（本级无属性提升；下一次属性提升在 {next_asi} 级）"
    if character.get("pending_subclass"):
        text += "；3 级解锁子职业，请在角色卡选择"
    if level in characters_mod.BOON_LEVELS:
        text += f"；获得{characters_mod.BOON_LEVELS[level]}，请在角色卡选择"
    if limits or track:
        text += "（法术栏位的调整可在角色卡或让 AI 代办）"
    return text


def settle_companion_spells(character):
    """
    同伴（无玩家决策）升级后的法术自动结算（分层模型）：
    法师消耗法术书名额补书并刷新准备列表；已知型补满已知；牧师/德鲁伊刷新准备；
    仪式与戏法补满。返回播报文本（无事发生返回空串）。
    """
    if spell_model(character) < 2 or not is_caster(character.get("class_id") or ""):
        return ""
    klass_id = character["class_id"]
    track = caster_track(klass_id)
    klass = srd.klass(klass_id)
    sources = set(klass.get("sources") or [])
    limits = limits_for(klass_id, character.get("level", 1), character)
    data = srd.spells()

    def eligible(pred):
        out = []
        for sid, entry in data.items():
            if int(entry.get("level", 0)) > limits["max_spell_level"]:
                continue
            if sources and not (set(entry.get("sources") or []) & sources):
                continue
            if has_spell(character, sid):
                continue
            if pred(entry):
                out.append((sid, entry))
        out.sort(key=lambda item: (int(item[1].get("level", 0)), item[0]))
        return out

    notes = []
    if track["spellbook"]:
        credits = int(character.get("spellbook_credits") or 0)
        if credits:
            pool = eligible(lambda e: int(e.get("level", 0)) >= 1)
            added = 0
            for sid, entry in pool[:credits]:
                learn_spell(character, sid, entry, bucket="spellbook")
                added += 1
            if added:
                notes.append(f"法术书自动补入 {added} 条")
        ordered = sorted(
            bucket_entries(character, "spellbook"),
            key=lambda e: (-int(e.get("level", 0)), e.get("name", "")),
        )
        character["prepared"] = [dict(e) for e in ordered[: limits["prepared"]]]
        rebuild_spell_union(character)
        notes.append(f"准备列表刷新为 {len(character['prepared'])} 条")
    if track["known"]:
        pool = eligible(lambda e: int(e.get("level", 0)) >= 1)
        room_count = max(0, int(limits["known"]) - len(bucket_entries(character, "known")))
        added = 0
        for sid, entry in pool[:room_count]:
            learn_spell(character, sid, entry, bucket="known")
            added += 1
        if added:
            notes.append(f"已知法术自动补入 {added} 条")
    if track["prepared"] and not track["spellbook"]:
        pool = eligible(lambda e: int(e.get("level", 0)) >= 1)
        picks = [{"id": sid, "name": entry.get("name", sid), "level": int(entry.get("level", 0))}
                 for sid, entry in pool[: limits["prepared"]]]
        character["prepared"] = picks
        rebuild_spell_union(character)
        notes.append(f"准备列表刷新为 {len(picks)} 条")
    if track["rituals"]:
        room_count = max(0, int(limits["rituals"]) - len(bucket_entries(character, "rituals")))
        pool = eligible(lambda e: bool(e.get("ritual")) and int(e.get("level", 0)) >= 1)
        added = 0
        for sid, entry in pool[:room_count]:
            learn_spell(character, sid, entry, bucket="rituals")
            added += 1
        if added:
            notes.append(f"仪式自动补入 {added} 条")
    if track["cantrips"]:
        room_count = max(0, int(limits["cantrips"]) - len(bucket_entries(character, "cantrips")))
        pool = eligible(lambda e: int(e.get("level", 0)) == 0)
        added = 0
        for sid, entry in pool[:room_count]:
            learn_spell(character, sid, entry, bucket="cantrips")
            added += 1
        if added:
            notes.append(f"戏法自动补入 {added} 个")
    rebuild_spell_union(character)
    return "；".join(notes)
