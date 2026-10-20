"""
角色创建与派生数值计算。

职责：把"建卡表单提交的原始选择"换算成"可以上桌的角色数据卡"。
所有 Black Flag 规则相关的计算都在这里：属性修正、血统/传承、技能/豁免熟练、
护甲与盾的 AC、1 级 HP、被动察觉、施法 DC/攻击加值。

本模块只做校验与计算，不接触房间状态（那是 state.py 的事）。
校验失败一律抛中文 ValueError，路由层直接把它当错误消息返回给浏览器。
"""

import re

from . import engine as engine_mod, spells as spells_mod, srd

# 六项属性的内部 id（英文缩写，供数据文件与前端共用）
ABILITIES = ["str", "dex", "con", "int", "wis", "cha"]
ABILITY_NAMES = {"str": "力量", "dex": "敏捷", "con": "体质", "int": "智力", "wis": "感知", "cha": "魅力"}

# 点买与标准数组：来自 data/srd/chargen.json（BFRD：32 点、区间 8-18；标准数组 16/14/14/13/10/8）
POINT_BUY_COSTS = {int(k): v for k, v in (srd.chargen().get("pointbuy_costs") or {}).items()}
POINT_BUY_TOTAL = int(srd.chargen().get("pointbuy_total") or 32)
STANDARD_ARRAY = list(srd.chargen().get("standard_array") or [16, 14, 14, 13, 10, 8])

# 护甲：数据驱动（data/srd/items.json 的 Armor 类条目，含 ac_base/ac_dex/is_shield）。
# 旧的硬编码表移除；为兼容旧字段（armor 存中文标签），保留标签→id 反查。
_ARMOR_LEGACY_ALIASES = {
    "chain_mail": "chain-mail", "chain_shirt": "chain-shirt",
    "studded": "studded-leather", "scale": "scale-mail",
}


def armor_items():
    """普通护甲表：{id: {"label","base","dex","category","is_shield"}}（dex='full' 或上限整数）。"""
    out = {}
    for idx, entry in srd.items().items():
        if entry.get("category") != "Armor":
            continue
        if entry.get("is_shield"):
            out[idx] = {"label": entry.get("name", idx), "base": int(entry.get("ac_base") or 2),
                        "dex": 0, "category": "盾牌", "is_shield": True}
            continue
        if entry.get("ac_base") is None:
            continue
        out[idx] = {"label": entry.get("name", idx), "base": int(entry["ac_base"]),
                    "dex": entry.get("ac_dex", "full"), "category": entry.get("category_zh", "护甲"),
                    "is_shield": False}
    return out


def resolve_armor_key(raw):
    """护甲 id / 旧字段标签 / 别名 → 规范 id；'none' 或无效值返回 'none'。"""
    key = str(raw or "none").strip()
    if not key or key == "none":
        return "none"
    key = _ARMOR_LEGACY_ALIASES.get(key, key)
    if key in armor_items():
        return key
    for idx, entry in armor_items().items():     # 旧角色按中文标签反查
        if entry["label"] == key:
            return idx
    return "none"


def worn_armor_key(character):
    """当前穿着护甲 id（优先 worn_armor，回退旧字段 armor 标签）。"""
    key = resolve_armor_key(character.get("worn_armor"))
    if key != "none":
        return key
    if character.get("worn_armor"):
        return key
    return resolve_armor_key(character.get("armor"))


def modifier(score):
    """属性值 → 修正值：(值-10)/2 向下取整（8→-1，15→+2）。"""
    return (score - 10) // 2


# 八大魔法学派（学派专精天赋的选择项）
SPELL_SCHOOLS = ["防护系", "咒法系", "预言系", "附魔系", "塑能系", "幻术系", "死灵系", "变化系"]

# 提升（Improvement）等级：BFRD 基准五档（战士等按进度表解析，多出的档位自动生效）
ASI_LEVELS = (4, 8, 12, 16, 19)


def improvement_levels(klass_id):
    """该职业获得「提升（Improvement）」的等级列表（读职业进度表的「提升」特性行）。"""
    try:
        klass = srd.klass(klass_id)
    except ValueError:
        return list(ASI_LEVELS)
    levels = sorted(
        int(lv) for lv, row in (klass.get("table") or {}).items()
        if any("提升" in f for f in row.get("features") or [])
    )
    return levels or list(ASI_LEVELS)


def talent_numbers(character):
    """
    数值型天赋的汇总效果（v0.5.4 机制化的 8 条 + 学派专精）：
      combat-conditioning 每等级 +2 HP；armor-expert 着中/重甲 +1 AC；
      armor-training 中甲敏捷上限 +3；aware 先攻 d20 下限 10；
      scrutinous 被动察觉/调查 +5；athletic 负重 ×18/推拉举 ×35；
      quick 速度 +10；school-specialization 所选学派施法 DC/攻击 +1。
    """
    talents = set(character.get("talents") or [])
    armor_key = worn_armor_key(character)
    category = "无甲" if armor_key == "none" else (armor_items().get(armor_key) or {}).get("category", "护甲")
    wearing_medium_heavy = category in ("中甲", "重甲")
    schools = character.get("talent_schools") or {}
    adaptation = str(character.get("adaptation") or "")
    return {
        "hp_per_level": 2 if "combat-conditioning" in talents else 0,
        "ac_bonus": 1 if ("armor-expert" in talents and wearing_medium_heavy) else 0,
        "medium_dex_cap": 3 if "armor-training" in talents else 2,
        "speed_bonus": 10 if "quick" in talents else 0,
        "passive_bonus": 5 if "scrutinous" in talents else 0,
        "init_floor": "aware" in talents,
        "carry_mult": 18 if "athletic" in talents else 15,
        "push_mult": 35 if "athletic" in talents else 30,
        "school": schools.get("school-specialization") or "",
        # 自然适应（兽裔/小族）：强健=无甲 AC 13+敏捷；其余为替代速度（展示级）
        "unarmored_base": 13 if adaptation == "强健" else 10,
        "alt_speed": {"禽类": "飞行", "灵巧": "攀爬", "水生": "游泳"}.get(adaptation, ""),
    }


def ac_value(dex_mod, armor_key, shield, numbers=None):
    """按护甲/盾/数值天赋计算 AC（建卡、重算、换装共用一处规则；护甲数据来自物品库）。"""
    numbers = numbers or {}
    key = resolve_armor_key(armor_key)
    if key == "none":
        base, dex_cap, category = int(numbers.get("unarmored_base", 10)), "full", "无甲"
    else:
        entry = armor_items().get(key) or {"base": 10, "dex": "full", "category": "无甲"}
        base, dex_cap, category = int(entry["base"]), entry["dex"], entry.get("category", "护甲")
    if dex_cap == "full":
        ac = base + dex_mod
    else:
        cap = int(dex_cap)
        if category == "中甲":                      # 护甲训练：中甲敏捷上限 2→3
            cap = max(cap, int(numbers.get("medium_dex_cap", 2)))
        ac = base + min(dex_mod, cap)
    if shield:
        ac += 2
    return ac + int(numbers.get("ac_bonus", 0))


def initiative_total(character, rolled_total):
    """先攻总值：「警觉」天赋把 d20 掷骰结果低于 10 的视为 10（下限即 10+敏捷修正）。"""
    numbers = talent_numbers(character)
    if numbers["init_floor"]:
        dex_mod = int((character.get("mods") or {}).get("dex", 0))
        return max(int(rolled_total), 10 + dex_mod)
    return int(rolled_total)


def luck_add(character, amount=1, d4_roll=None, from_failure=False):
    """
    幸运增加（BFRD 幸运系统）：失败获得时「幸运之触」翻倍；
    已有 5 点再获得时不累加，而是掷 d4 重置（幸运之触为 1d4+1，d4 由调用方掷好传入）。
    返回 (新幸运值, 说明文本)。
    """
    luck = max(0, int(character.get("luck") or 0))
    touch = "touch-of-luck" in (character.get("talents") or [])
    gain = int(amount) * (2 if (from_failure and touch and int(amount) == 1) else 1)
    if luck >= 5:
        reset = int(d4_roll or 0) + (1 if touch else 0)
        character["luck"] = max(1, reset)
        return character["luck"], f"幸运已满，重置为 {character['luck']} 点"
    character["luck"] = min(5, luck + gain)
    return character["luck"], f"幸运 +{gain}（当前 {character['luck']}/5）"


def luck_spend(character, amount=1):
    """消耗幸运（1 对 1 加值 / 3 点重掷等由叙事决定，引擎只做余额校验与扣减）。"""
    luck = max(0, int(character.get("luck") or 0))
    amount = int(amount)
    if amount <= 0:
        raise ValueError("消耗数量必须为正")
    if amount > luck:
        raise ValueError(f"幸运不足（当前 {luck} 点，需要 {amount} 点）")
    character["luck"] = luck - amount
    return character["luck"]


# ---------------------------------------------------------------------------
# 生命状态（濒死 / 稳定 / 死亡）与力竭（BFRD 附录规则，引擎自动结算）
# ---------------------------------------------------------------------------

REVIVAL_SPELLS = ("revivify", "raise-dead", "resurrection", "true-resurrection")   # 复苏术/死者复活/复活术/真实复活术


def life_state(character):
    """返回 (状态键, 中文说明)：alive / dying / stable / dead。"""
    if character.get("dead"):
        return "dead", "已死亡"
    if int(character["hp"]["cur"] or 0) <= 0:
        if character.get("stable"):
            return "stable", "稳定（昏迷）"
        if character.get("dying"):
            return "dying", "濒死（昏迷）"
    return "alive", ""


def start_dying(character, clock_minutes=0.0):
    """进入濒死：计数清零、挂上昏迷与倒地状态（-1 HP 时由 HP 结算调用）。"""
    character["dying"] = True
    character["stable"] = False
    character["death_saves"] = {"successes": 0, "failures": 0}
    character["stable_until"] = None
    effects = character.setdefault("effects", [])
    for cond in ("昏迷", "倒地"):
        if not any(e.get("name") == cond for e in effects):
            spells_mod.add_effect(character, cond, 0, clock_minutes, note="濒死自动状态")
    name = character.get("name", "")
    return f"{name} 陷入昏迷与濒死（其回合开始时自动掷死亡豁免：d20 ≥10 成功，3 成功稳定 / 3 失败死亡）"


def stop_dying(character):
    """脱离濒死（回复生命/复活）：清计数、移除昏迷与倒地状态。"""
    was = character.get("dying") or character.get("stable")
    character["dying"] = False
    character["stable"] = False
    character["stable_until"] = None
    character["death_saves"] = {"successes": 0, "failures": 0}
    character["effects"] = [
        e for e in (character.get("effects") or []) if e.get("name") not in ("昏迷", "倒地")
    ]
    return bool(was)


def mark_dead(character):
    """死亡（死亡豁免三败 / 巨量伤害 / 力竭 6 级）：解除濒死，留昏迷标记。"""
    character["dead"] = True
    character["dying"] = False
    character["stable"] = False
    character["stable_until"] = None
    character["death_saves"] = {"successes": 0, "failures": 0}
    return f"{character.get('name', '')} 已死亡（需要复苏术 / 死者复活 / 复活术 / 真实复活术才能重返人世）"


def death_save_roll(character, d20_total, clock_minutes=0.0):
    """
    结算一次死亡豁免（d20 由调用方掷好传入；掷 1 计两次失败、掷 20 回复 1 HP）。
    返回 (播报文本列表, 事件键列表)：事件键如 success/failure/stable/dead/recover。
    """
    name = character.get("name", "")
    saves = character.setdefault("death_saves", {"successes": 0, "failures": 0})
    texts, events = [f"{name} 死亡豁免：d20 = {d20_total}"], []
    if int(d20_total) >= 20:
        character["hp"]["cur"] = max(1, int(character["hp"]["cur"]))
        stop_dying(character)
        texts.append("掷出 20：回复 1 点生命值并恢复意识（解除昏迷与濒死）")
        events.append("recover")
        return texts, events
    failures = 2 if int(d20_total) <= 1 else 0
    if int(d20_total) >= 10:
        saves["successes"] = int(saves.get("successes", 0)) + 1
        texts.append(f"成功（{saves['successes']}/3）")
        events.append("success")
    else:
        add = failures or 1
        saves["failures"] = int(saves.get("failures", 0)) + add
        texts.append(f"失败{f'（掷出 1：计两次失败）' if add == 2 else ''}（{saves['failures']}/3）")
        events.append("failure")
    if int(saves["successes"]) >= 3:
        character["stable"] = True
        character["dying"] = False
        character["death_saves"] = {"successes": 0, "failures": 0}
        # 稳定者 1d4 小时后回复 1 HP（时间由调用方滚动 d4 后写入；此处仅标记待恢复）
        character["stable_until"] = None
        texts.append("累计三次成功：状态变为稳定（无需继续死亡豁免，1d4 小时后自动回复 1 HP）")
        events.append("stable")
    elif int(saves["failures"]) >= 3:
        texts.append(mark_dead(character))
        events.append("dead")
    return texts, events


def damage_while_dying(character, crit=False):
    """0 HP 期间再受伤：自动失败一次（重击两次）；三败即死亡。返回播报文本。"""
    if character.get("dead"):
        return ""
    saves = character.setdefault("death_saves", {"successes": 0, "failures": 0})
    add = 2 if crit else 1
    saves["failures"] = int(saves.get("failures", 0)) + add
    text = f"{character.get('name', '')} 在 0 HP 状态下再受伤：死亡豁免自动失败{'两次（重击）' if crit else '一次'}（{saves['failures']}/3）"
    if int(saves["failures"]) >= 3:
        text += "；" + mark_dead(character)
    return text


def set_stable(character, stable_until=None):
    """被稳定（成功稳定化或三次死亡豁免成功）：清除计数，等待 1d4 小时自动恢复。"""
    character["stable"] = True
    character["dying"] = False
    character["death_saves"] = {"successes": 0, "failures": 0}
    character["stable_until"] = round(float(stable_until), 1) if stable_until is not None else None
    name = character.get("name", "")
    return f"{name} 被稳定：保持昏迷（0 HP），无需死亡豁免" + (f"，将于游戏时间约 {int(float(stable_until) - float(stable_until) % 60)} 分钟后自行苏醒" if stable_until else "")


# 力竭（六档累计：1 属性检定劣势 / 2 速度减半 / 3 攻击与豁免劣势 / 4 HP 上限减半 / 5 速度 0 / 6 死亡）
def effective_hp_max(character):
    """当前有效 HP 上限（力竭 4 级减半；基准存 hp_max_base）。"""
    base = int(character.get("hp_max_base") or (character.get("hp") or {}).get("max") or 1)
    return max(1, base // 2) if int(character.get("exhaustion") or 0) >= 4 else base


def apply_exhaustion(character, level):
    """设置力竭等级并即时生效（速度/生命上限重算；6 级即死）。返回播报文本。"""
    try:
        level = max(0, min(6, int(level)))
    except (TypeError, ValueError):
        raise ValueError("力竭等级不合法")
    old = int(character.get("exhaustion") or 0)
    character["exhaustion"] = level
    hp = character.setdefault("hp", {"max": 1, "cur": 1})
    character.setdefault("hp_max_base", int(hp.get("max") or 1))
    recompute_derived(character)                     # 速度按新力竭等级重算
    hp["max"] = effective_hp_max(character)
    if int(hp.get("cur") or 0) > int(hp["max"]):
        hp["cur"] = int(hp["max"])
    name = character.get("name", "")
    effects = {
        1: "属性检定劣势", 2: "速度减半", 3: "攻击与豁免劣势",
        4: "生命值上限减半", 5: "速度为 0", 6: "死亡",
    }
    text = f"{name} 力竭 {old} → {level} 级（当前：{'；'.join(effects[i] for i in range(1, level + 1)) or '无'}）"
    if level >= 6 and not character.get("dead"):
        text += "；" + mark_dead(character)
    return text


# 护甲类别由物品库数据提供（armor_items()）；穿甲施法校验见 armor_proficiency_warning。


def armor_proficiency_warning(character):
    """
    穿甲施法校验（BFRD：必须熟练所穿护甲才能施法）。
    返回警示文本（''=无问题）；平台为信任制，只提醒不阻断。
    """
    try:
        klass = srd.klass(character.get("class_id") or "")
    except ValueError:
        return ""
    armor_key = worn_armor_key(character)
    category = "无甲" if armor_key == "none" else (armor_items().get(armor_key) or {}).get("category", "")
    shield_worn = bool(character.get("worn_shield", character.get("shield")))
    prof_text = str(klass.get("armor_zh") or "")
    all_ok = any(w in prof_text for w in ("所有", "全部", "任意"))
    problems = []
    if category not in ("", "无甲", "盾牌") and not (all_ok or category in prof_text):
        problems.append(category)
    if shield_worn and not (all_ok or "盾牌" in prof_text):
        problems.append("盾牌")
    if not problems:
        return ""
    worn_label = "无甲" if armor_key == "none" else (armor_items().get(armor_key) or {}).get("label", armor_key)
    return (
        f"注意：{character.get('name', '')} 不熟练所穿装备（{'、'.join(problems)}；当前穿着：{worn_label}"
        f"{' + 盾' if shield_worn else ''}；职业护甲熟练：{prof_text or '无'}），"
        f"按规则无法施法（信任制未阻断，请与 GM 确认）"
    )


def skill_check(character, skill_id=None, ability=None, dc=None, mode="normal"):
    """
    通用技能/属性检定（引擎权威）：从角色卡取调整值（属性修正 + 熟练加值），
    力竭 1 级起自动给属性检定施加劣势；返回结构化结果 dict。
    """
    all_skills = srd.skills()
    if skill_id:
        entry = all_skills.get(str(skill_id))
        if not entry:
            raise ValueError(f"未知技能：{skill_id}")
        ability = entry["ability"]
        prof = int(character.get("prof_bonus") or 2)
        bonus = int((character.get("mods") or {}).get(ability, 0)) + (prof if skill_id in (character.get("skills") or {}) else 0)
        label = entry["name"]
    else:
        ability = str(ability or "")
        if ability not in ABILITIES:
            raise ValueError("需要指定技能或属性")
        bonus = int((character.get("mods") or {}).get(ability, 0))
        label = ABILITY_NAMES[ability]
    exhaustion = int(character.get("exhaustion") or 0)
    if mode == "normal" and exhaustion >= 1:
        mode = "disadvantage"                          # 力竭 1 级：属性检定劣势
    check = engine_mod.roll_check(modifier=bonus, dc=int(dc) if dc is not None else None, mode=mode, label=label)
    check["exhaustion"] = exhaustion
    check["skill"] = skill_id
    check["ability"] = ability
    return check


def point_buy_cost(scores):
    """计算一组基础属性的点买总花费。"""
    return sum(POINT_BUY_COSTS[v] for v in scores.values())


def equipment_to_items(names):
    """出身装备字符串 → 背包条目（背包系统的初始内容）：
    命中物品库的带 id（悬停说明/定价买卖可用），否则记作自由文本物品。"""
    db = srd.items()
    out = []
    for name in names or []:
        idx = srd.item_id_by_name(name)
        if idx:
            out.append({"id": idx, "name": db[idx].get("name_zh", str(name)), "qty": 1})
        else:
            out.append({"id": None, "name": str(name), "qty": 1})
    return out


# ---------------------------------------------------------------------------
# 职业初始装备的选项组（BFRD「（a）……或（b）……」）：向导选择器与引擎结算的唯一规则来源
# ---------------------------------------------------------------------------

_ZH_COUNT = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_OPTION_MARK = re.compile(r"（[_ ]*([a-zA-Z])[_ ]*）")
_TOKEN_SPLIT = re.compile(r"[、和]")
_WEAPON_PROMPT = re.compile(r"^(?:([一二两三四五六七八九十\d]+)\s*[件把根])?\s*(任意)?(军用|简易)(近战)?武器$")
_ITEM_COUNT = re.compile(r"^(?:(\d+)|([一二两三四五六七八九十]+))\s*(?:支|把|面|个|件|套|根|瓶|本|张|只|副|枚|条|块|包|份|顶|辆)?\s*(.+)$")
_BUNDLE_IDS = {"20-arrows-20", "20-crossbow-bolts-20"}    # 库内条目本身即"20 支装"


def mundane_weapons():
    """物品库中的普通武器（简易/军用）：车卡武器选择器与引擎校验的数据源（类别如「军用近战」）。"""
    return {
        idx: entry
        for idx, entry in srd.items().items()
        if str(entry.get("weapon_category_zh", "")).startswith(("简易", "军用"))
    }


def _is_melee_weapon(entry):
    """武器条目是否近战：类别串含「远程」即远程。"""
    return "远程" not in str(entry.get("weapon_category_zh", ""))


def _split_equipment_line(line):
    """一行职业装备 → [(选项键, 文本)]；支持（_a_）/（_b_）/（_c_）多选项；无标记返回单选项（键为空串）。"""
    text = str(line)
    marks = list(_OPTION_MARK.finditer(text))
    if not marks:
        return [("", text.strip().strip("或、").strip())]
    choices = []
    for i, mark in enumerate(marks):
        key = mark.group(1).lower()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        seg = text[mark.end():end].strip()
        seg = re.sub(r"[、或]\s*$", "", seg).strip()          # 标记前残留的连接词归上一段
        choices.append((key, seg))
    return choices


def _parse_weapon_prompt(token):
    """「两件军用武器 / 任意简易近战武器」→ 武器选择位；不是则返回 None。"""
    m = _WEAPON_PROMPT.match(token.strip())
    if not m:
        return None
    count = _ZH_COUNT.get(m.group(1), 1) if m.group(1) else 1
    return {
        "label": token.strip(),
        "category": "军用" if m.group(3) == "军用" else "简易",
        "melee": bool(m.group(4)),
        "count": count,
    }


def _parse_equipment_token(token):
    """单个物品 token → {"id"/"name"/"qty"}；「两把短剑」「20 支箭」会拆出数量。"""
    token = token.strip(" ，,。；;")
    if not token:
        return None
    m = _ITEM_COUNT.match(token)
    if m:
        count = int(m.group(1)) if m.group(1) else _ZH_COUNT.get(m.group(2), 1)
        name = m.group(3).strip()
    else:
        count, name = 1, token
    idx = srd.item_id_by_name(name)
    if not idx:
        # 名称带括注（如「战锤（如具有熟练）」）时，去括注再查一次
        cleaned = re.sub(r"（.*?）", "", name)
        cleaned = re.sub(r"（[^）]*$", "", cleaned).strip()
        idx = srd.item_id_by_name(cleaned)
    if idx and idx in _BUNDLE_IDS and count == 20:
        count = 1                                     # 「20 支箭」= 库内 20 支装，数量不再叠加
    if idx:
        return {"id": idx, "name": srd.items()[idx].get("name", name), "qty": count}
    return {"id": None, "name": name, "qty": count}


def _parse_option_side(text):
    """一侧选项文本 → (具体物品列表, 武器选择位列表)。"""
    items, prompts = [], []
    for token in _TOKEN_SPLIT.split(text):
        token = token.strip()
        if not token:
            continue
        prompt = _parse_weapon_prompt(token)
        if prompt:
            prompts.append(prompt)
            continue
        item = _parse_equipment_token(token)
        if item:
            items.append(item)
    return items, prompts


def equipment_specs(class_id):
    """
    解析职业初始装备，返回向导用选项组：
      [{"index": 行号, "choices": [{"key": "a"/"b"/"", "text": 选项原文,
        "items": [{"id","name","qty"}...], "weapon_picks": [{"label","category","melee","count"}...]}]}]
    每行至少一个选项；无（b）的行 key 为空串（如「一本法术书」）。
    """
    klass = srd.klass(class_id)
    specs = []
    for index, line in enumerate(klass.get("equipment_lines") or []):
        choices = []
        for key, text in _split_equipment_line(line):
            items, prompts = _parse_option_side(text)
            choices.append({"key": key, "text": text, "items": items, "weapon_picks": prompts})
        specs.append({"index": index, "choices": choices})
    return specs


def resolve_equipment(class_id, choices):
    """
    校验并结算建卡向导提交的装备选择（引擎侧唯一权威）。
    choices 形如 [{"index": 行号, "key": "a"/"b"/"", "weapons": [武器 id...]}]：
    行数必须与职业装备行一致；key 必须属于该行；weapons 与选择位的数量、
    类别（军用/简易）与近战要求逐一对应。返回 (物品列表, 装备文本列表)。
    """
    klass = srd.klass(class_id)
    specs = equipment_specs(class_id)
    if not isinstance(choices, list) or len(choices) != len(specs):
        got = len(choices) if isinstance(choices, list) else 0
        raise ValueError(f"装备选择需要 {len(specs)} 行（收到 {got} 行）")
    weapons = mundane_weapons()
    items, texts = [], []
    for spec, choice in zip(specs, choices):
        by_key = {c["key"]: c for c in spec["choices"]}
        key = str(choice.get("key", ""))
        if key not in by_key:
            key = spec["choices"][0]["key"]           # 缺省取第一项（容错旧客户端）
        side = by_key[key]
        items.extend(dict(item) for item in side["items"])
        pick_ids = [str(w) for w in (choice.get("weapons") or [])]
        expect = sum(p["count"] for p in side["weapon_picks"])
        if len(pick_ids) != expect:
            raise ValueError(f"「{side['text']}」需要选择 {expect} 件武器（收到 {len(pick_ids)} 件）")
        cursor = 0
        for prompt in side["weapon_picks"]:
            for _ in range(prompt["count"]):
                wid = pick_ids[cursor]
                cursor += 1
                entry = weapons.get(wid)
                if not entry:
                    raise ValueError(f"初始武器选择不合法：{wid}（请从普通武器中选择）")
                if not str(entry.get("weapon_category_zh", "")).startswith(prompt["category"]):
                    raise ValueError(f"「{entry.get('name', wid)}」不属于{prompt['category']}武器")
                if prompt["melee"] and not _is_melee_weapon(entry):
                    raise ValueError(f"「{entry.get('name', wid)}」不是近战武器")
                items.append({"id": wid, "name": entry.get("name", wid), "qty": 1})
        texts.append(side["text"])
    return items, texts


def _initial_coins(background):
    """初始钱袋：背景装备文本中的「N 金币」（如腰包）计入 gp。"""
    coins = {"pp": 0, "gp": 0, "sp": 0, "cp": 0}
    m = re.search(r"(\d+)\s*金币", str(background.get("equipment_zh") or ""))
    if m:
        coins["gp"] = int(m.group(1))
    return coins


def recompute_derived(character):
    """按当前 abilities/mods/职业/等级重算全部派生数值（ASI 后调用；规则与建卡一致）。"""
    klass = {}
    if character.get("class_id"):
        try:
            klass = srd.klass(character["class_id"])
        except ValueError:
            klass = {}
    prof = spells_mod.PROF_BY_LEVEL.get(int(character.get("level", 1)), 2)
    mods = {a: modifier(int(v)) for a, v in (character.get("abilities") or {}).items()}
    character["mods"] = mods
    character["prof_bonus"] = prof
    character["saves"] = {
        a: mods.get(a, 0) + (prof if a in klass.get("saves", []) else 0) for a in mods
    }
    all_skills = srd.skills()
    character["skills"] = {
        sid: mods.get(all_skills[sid]["ability"], 0) + prof
        for sid in (character.get("skills") or {})
        if sid in all_skills
    }
    numbers = talent_numbers(character)
    character["passive_perception"] = (
        10 + mods.get("wis", 0) + (prof if "perception" in character["skills"] else 0) + numbers["passive_bonus"]
    )
    # 速度：血统基础 + 数值天赋；力竭 2 级减半、5 级为 0（基准存 speed_base）
    race_id = str(character.get("race_id") or "")
    if race_id in srd.races():
        speed_base = int(srd.races()[race_id]["speed"]) + numbers["speed_bonus"]
        character["speed_base"] = speed_base
        exhaustion = int(character.get("exhaustion") or 0)
        if exhaustion >= 5:
            character["speed"] = 0
        elif exhaustion >= 2:
            character["speed"] = max(5, speed_base // 2)
        else:
            character["speed"] = speed_base
    # AC：按当前穿着（worn_armor/worn_shield，旧字段 armor/shield 兼容）重算
    armor_key = worn_armor_key(character)
    character["worn_armor"] = armor_key
    character["shield"] = bool(character.get("worn_shield", character.get("shield")))
    character["worn_shield"] = character["shield"]
    character["armor"] = "无甲" if armor_key == "none" else (armor_items().get(armor_key) or {}).get("label", "无甲")
    character["ac"] = ac_value(mods.get("dex", 0), armor_key, bool(character["shield"]), numbers)
    if character.get("spellcasting"):
        ability = character["spellcasting"]["ability"]
        character["spellcasting"] = {
            "ability": ability,
            "save_dc": 8 + prof + mods.get(ability, 0),
            "attack_bonus": prof + mods.get(ability, 0),
        }


def apply_asi(character, asi, talent_id=None, school=None):
    """
    应用一次属性提升（BFRD 的 Improvement，4/8/12/16/19 级获得）：
      两式：{"str": 2}（一项 +2）或 {"str": 1, "dex": 1}（两项各 +1），合计 2 点、单项上限 20；
      第三式：传入 talent_id 时为「一项 +1 + 从本职业提升天赋列表中选一项天赋」
      （学派专精另需 school 指定八大魔法学派之一）。
    重算派生数值；体质提升按"每级回补 1 HP"补齐既往等级；数值型天赋即时生效。返回中文摘要。
    """
    if not isinstance(asi, dict) or not asi:
        raise ValueError("请指定属性提升方案（如 {\"str\": 2} 或 {\"str\": 1, \"dex\": 1}）")
    choices = {}
    for key, value in asi.items():
        if key not in ABILITIES:
            raise ValueError(f"未知属性：{key}")
        try:
            amount = int(value)
        except (TypeError, ValueError):
            raise ValueError("属性提升数值不合法")
        if amount > 0:
            choices[key] = choices.get(key, 0) + amount
    total = sum(choices.values())
    if talent_id:
        # 第三式：属性仅 +1 一项，另附一项职业提升天赋
        if total != 1 or len(choices) != 1:
            raise ValueError("选择天赋时只能有一项属性 +1")
        klass = {}
        try:
            klass = srd.klass(character.get("class_id"))
        except ValueError:
            klass = {}
        lists = klass.get("improvement_talents") or []
        valid = {tid for tid, t in srd.talents().items() if t.get("list") in lists}
        if talent_id not in valid:
            raise ValueError("该天赋不在本职业的提升天赋列表内")
        if talent_id in (character.get("talents") or []):
            raise ValueError("该天赋已经拥有")
        if talent_id == "school-specialization":
            if school not in SPELL_SCHOOLS:
                raise ValueError("「学派专精」需要选择八大魔法学派之一")
    elif total != 2:
        raise ValueError("属性提升必须合计 +2（一项 +2，或两项各 +1）")
    for key, amount in choices.items():
        if int(character["abilities"][key]) + amount > 20:
            raise ValueError(f"{ABILITY_NAMES[key]} 已达 20 上限，不能继续提升")
    old_con_mod = int((character.get("mods") or {}).get("con", 0))
    for key, amount in choices.items():
        character["abilities"][key] = int(character["abilities"][key]) + amount
    if talent_id:
        talents = list(character.get("talents") or [])
        talents.append(talent_id)
        character["talents"] = talents
        if school:
            schools = dict(character.get("talent_schools") or {})
            schools[talent_id] = school
            character["talent_schools"] = schools
    recompute_derived(character)
    # 体质提升：正规规则按既往等级回补 HP（每级每 +1 修正 +1 HP）
    hp_gain = (int(character["mods"].get("con", 0)) - old_con_mod) * int(character.get("level", 1))
    # 战斗锤炼（每角色等级 +2 HP）：获得时按当前等级一次性补足
    level = int(character.get("level", 1))
    if talent_id == "combat-conditioning":
        hp_gain += 2 * level
    if hp_gain > 0:
        character["hp"]["max"] += hp_gain
        character["hp"]["cur"] += hp_gain
    character["pending_asi"] = max(0, int(character.get("pending_asi", 0)) - 1)
    parts = "、".join(f"{ABILITY_NAMES[k]} +{v}" for k, v in choices.items())
    suffix = ""
    if talent_id:
        talent = srd.talents().get(talent_id) or {}
        suffix = f"，并获得天赋「{talent.get('name', talent_id)}」"
        if school:
            suffix += f"（{school}）"
    return f"{character.get('name', '')} 提升：{parts}（合计 +{total}）{suffix}"


def auto_asi_choice(character):
    """同伴等无玩家角色的自动 ASI：优先主属性 +2（≤18 时），否则前两项各 +1；都满返回 None。"""
    try:
        klass = srd.klass(character.get("class_id"))
    except ValueError:
        klass = {}
    primary = list(klass.get("primary") or [])
    order = primary + [a for a in ABILITIES if a not in primary]
    abilities = character.get("abilities") or {}
    for key in order:
        if int(abilities.get(key, 0)) <= 18:
            return {key: 2}
    picks = [a for a in order if int(abilities.get(a, 0)) <= 19][:2]
    if len(picks) == 2:
        return {picks[0]: 1, picks[1]: 1}
    return None


def settle_companion_asi(character):
    """把同伴所有待分配 ASI 按自动策略结算；返回播报文本（无待分配返回空串）。"""
    texts = []
    while int(character.get("pending_asi", 0)) > 0:
        choice = auto_asi_choice(character)
        if not choice:
            character["pending_asi"] = 0     # 属性全满，作废
            texts.append(f"{character.get('name', '')} 属性已满，本次提升作废")
            break
        texts.append(apply_asi(character, choice))
    return "；".join(texts)


# BFRD：子职业在 3 级选择；10 级英雄恩赐、20 级史诗恩赐（各职业给出的选项不同）
SUBCLASS_LEVEL = 3
BOON_LEVELS = {10: "英雄恩赐", 20: "史诗恩赐"}


def choose_subclass(character, subclass_id):
    """选择子职业（3 级解锁；引擎校验等级与归属）。返回中文摘要。"""
    level = int(character.get("level") or 1)
    if level < SUBCLASS_LEVEL:
        raise ValueError(f"子职业要到 {SUBCLASS_LEVEL} 级才能选择（当前 {level} 级）")
    klass = srd.klass(character.get("class_id"))
    subclasses = klass.get("subclasses") or {}
    sub = subclasses.get(str(subclass_id))
    if not sub:
        raise ValueError(f"未知子职业：{subclass_id}（本职业可选：{ '、'.join(s.get('name', k) for k, s in subclasses.items()) }）")
    if character.get("subclass"):
        raise ValueError(f"已经选择过子职业（{character.get('subclass_name') or character['subclass']}）")
    character["subclass"] = str(subclass_id)
    character["subclass_name"] = sub.get("name", subclass_id)
    character["pending_subclass"] = False
    name = character.get("name", "")
    return f"{name} 选择了子职业「{sub.get('name', subclass_id)}」"


def boon_options(klass_id, level):
    """
    解析某职业在 10/20 级的恩赐选项：从职业特性描述里的「- **名称。** 说明」条目提取。
      [{"key": "0", "name": 名称, "desc": 说明}]
    没有可解析条目时返回空列表（例如译文格式差异）。
    """
    try:
        klass = srd.klass(klass_id)
    except ValueError:
        return []
    feature = None
    for f in (klass.get("features") or {}).values():
        if int(f.get("level") or 0) == int(level):
            feature = f
            break
    if not feature:
        return []
    options = []
    # 条目两种写法：「- **名称。** 说明」或「- 名称。说明」
    for m in re.finditer(r"^-\s*(?:\*\*(.+?)\*\*|(.+?)。)\s*(.+)$", feature.get("desc") or "", re.M):
        raw_name = m.group(1) or m.group(2)
        options.append({
            "key": str(len(options)),
            "name": raw_name.strip().rstrip("。"),
            "desc": m.group(3).strip(),
        })
    return options


def choose_boon(character, level, boon_key):
    """选择 10 级英雄恩赐 / 20 级史诗恩赐（选项来自职业特性文本）。返回中文摘要。"""
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise ValueError("恩赐等级不合法")
    pending = [int(x) for x in (character.get("pending_boons") or [])]
    if level not in pending:
        raise ValueError(f"当前没有待选的 {level} 级恩赐")
    options = boon_options(character.get("class_id"), level)
    option = next((o for o in options if o["key"] == str(boon_key)), None)
    if not option:
        raise ValueError("未知恩赐选项")
    boons = list(character.get("boons") or [])
    boons.append({"level": level, "key": option["key"], "name": option["name"], "desc": option["desc"]})
    character["boons"] = boons
    character["pending_boons"] = [x for x in pending if x != level]
    name = character.get("name", "")
    return f"{name} 获得{BOON_LEVELS.get(level, '恩赐')}「{option['name']}」：{option['desc']}"


def settle_companion_progression(character):
    """
    同伴无玩家时的升级选择自动结算：子职业取数据第一项、恩赐按等级取第一项。
    返回播报文本（无待决返回空串）。
    """
    texts = []
    if character.get("pending_subclass"):
        try:
            klass = srd.klass(character.get("class_id"))
        except ValueError:
            klass = {}
        first = next(iter((klass.get("subclasses") or {}).items()), None)
        if first:
            texts.append(choose_subclass(character, first[0]))
        else:
            character["pending_subclass"] = False
    for level in [int(x) for x in (character.get("pending_boons") or [])]:
        options = boon_options(character.get("class_id"), level)
        if options:
            texts.append(choose_boon(character, level, options[0]["key"]))
        else:
            character["pending_boons"] = [x for x in character["pending_boons"] if int(x) != level]
    return "；".join(texts)


def carry_info(char):
    """
    负重计算（SRD 变体规则；仅供界面与 AI 提示，不自动改速度等数值）：
      携带上限 = 力量 × 15 磅；推、拉、举 = 力量 × 30。
      超过 力量×5 → 负重；超过 力量×10 → 重度负重；超过上限 → 无法携带。
    物品重量取自物品库；自定义物品重量未知时不计入并计数提示。
    硬币按通用称重规则：任何面值都是 50 枚 = 1 磅（按枚数，不看面值）。
    """
    db = srd.items()
    items_lb = 0.0
    unknown = 0
    for item in char.get("items") or []:
        entry = db.get(item.get("id") or "") if item.get("id") else None
        weight = entry.get("weight") if entry else None
        if weight is None:
            unknown += 1
            continue
        items_lb += float(weight) * int(item.get("qty", 1))
    coin_count = sum(max(0, int(v or 0)) for v in (char.get("coins") or {}).values())
    coins_lb = coin_count / 50.0
    total = round(items_lb + coins_lb, 1)
    str_score = int((char.get("abilities") or {}).get("str", 10))
    numbers = talent_numbers(char)
    capacity = str_score * numbers["carry_mult"]
    if total > capacity:
        level, label, hint = "over", "超过上限", "无法携带全部物品"
    elif total > str_score * 10:
        level, label, hint = "heavy", "重度负重", "速度 −20 尺；力量/敏捷/体质相关检定与攻击劣势"
    elif total > str_score * 5:
        level, label, hint = "encumbered", "负重", "速度 −10 尺"
    else:
        level, label, hint = "normal", "正常", ""
    return {
        "weight": total,
        "items_weight": round(items_lb, 1),
        "coins_weight": round(coins_lb, 2),
        "coin_count": coin_count,
        "capacity": capacity,
        "push": str_score * numbers["push_mult"],
        "level": level,
        "label": label,
        "hint": hint,
        "unknown_count": unknown,
    }


# 四级货币（10 铜 = 1 银，10 银 = 1 金，10 金 = 1 铂；内部价值一律折算为铜）
COIN_RATES_CP = {"cp": 1, "sp": 10, "gp": 100, "pp": 1000}
COIN_ORDER = ("pp", "gp", "sp", "cp")
COIN_LABELS = {"pp": "铂金币", "gp": "金币", "sp": "银币", "cp": "铜币"}


def coin_total_cp(coins):
    """四级硬币 → 总价值（铜币整数）。"""
    total = 0
    for key, rate in COIN_RATES_CP.items():
        try:
            total += max(0, int((coins or {}).get(key) or 0)) * rate
        except (TypeError, ValueError):
            continue
    return total


def coins_from_cp(total_cp):
    """总价值（铜）→ 最优四级硬币组合（贪婪拆分：先大额后小额）。"""
    rest = max(0, int(total_cp))
    out = {}
    for key in COIN_ORDER:
        rate = COIN_RATES_CP[key]
        out[key] = rest // rate
        rest %= rate
    return out


def coins_text(coins):
    """四级硬币 → 中文显示（只显示非零项；全零显示 0 铜币）。"""
    parts = []
    for key in COIN_ORDER:
        try:
            count = max(0, int((coins or {}).get(key) or 0))
        except (TypeError, ValueError):
            count = 0
        if count:
            parts.append(f"{count} {COIN_LABELS[key]}")
    return " ".join(parts) if parts else "0 铜币"


def coins_add_cp(coins, delta_cp):
    """收款：把增额按最优组合并入钱袋（不动既有各档枚数，保留玩家的分档习惯）。"""
    result = {key: max(0, int((coins or {}).get(key) or 0)) for key in COIN_ORDER}
    for key, count in coins_from_cp(max(0, int(delta_cp))).items():
        result[key] += count
    return result


def coins_pay_cp(coins, cost_cp):
    """
    付款：先用小额硬币整枚支付（不动大额），不足时把最小的一枚大额硬币"找开"、
    余额作为零钱找回；保留既有分档。余额不足抛中文 ValueError。
    """
    result = {key: max(0, int((coins or {}).get(key) or 0)) for key in COIN_ORDER}
    total = coin_total_cp(result)
    cost_cp = max(0, int(cost_cp))
    if total < cost_cp:
        raise ValueError(f"钱不够：还差 {cost_cp - total} 铜币")
    need = cost_cp
    # 第一轮：从小到大，只花整枚硬币
    for key in ("cp", "sp", "gp", "pp"):
        rate = COIN_RATES_CP[key]
        take = min(result[key], need // rate)
        result[key] -= take
        need -= take * rate
    # 第二轮：还差零头且小额不够 → 找开当前最小的一枚大额硬币，找零归入小额
    if need > 0:
        for key in ("cp", "sp", "gp", "pp"):
            if result[key] > 0:
                result[key] -= 1
                change = COIN_RATES_CP[key] - need
                for ck, count in coins_from_cp(change).items():
                    result[ck] += count
                break
    return result


def coins_change_cp(coins, delta_cp):
    """
    按价值增减硬币（收入并档、支出找零，均保留既有分档）。
    delta_cp 为铜币价值，可为负；余额不足时抛中文 ValueError。返回新的四格硬币字典。
    """
    delta = int(delta_cp)
    if delta >= 0:
        return coins_add_cp(coins, delta)
    return coins_pay_cp(coins, -delta)


def compute_character(payload):
    """
    入口函数：校验表单并生成完整角色卡（dict）。

    payload 来自 /api/create 的 JSON：
      name/race/klass/background/method/base/skills/armor/shield/backstory
    其中 base 是"未加种族加成的六项属性"；最终值 = base + 种族加成。
    """
    # ---- 基础校验：名字与三选一 ----
    name = str(payload.get("name", "")).strip()[:30]
    if not name:
        raise ValueError("角色名不能为空")
    gender = str(payload.get("gender", "")).strip()[:10]    # 性别（可选，仅叙事用）
    race_id = str(payload.get("race", ""))
    class_id = str(payload.get("klass", ""))
    background_id = str(payload.get("background", ""))
    race = srd.race(race_id)          # 这里是 BFRD 的"血统"
    klass = srd.klass(class_id)
    background = srd.background(background_id)
    heritage_id = str(payload.get("heritage", ""))
    heritage = srd.heritage(heritage_id) if heritage_id else None

    # ---- 自然适应（兽裔/小族）：必须在该血统的适应列表中选一项 ----
    adaptations = race.get("adaptations") or []
    adaptation = str(payload.get("adaptation", "")).strip()
    if adaptations:
        names = [a.get("name", "") for a in adaptations]
        if adaptation not in names:
            raise ValueError(f"「{race['name']}」需要在自然适应中选择一项：{' / '.join(names)}")
    elif adaptation:
        raise ValueError("该血统没有自然适应")
    adaptation_spell_ability = ""
    if adaptation == "侏儒":
        adaptation_spell_ability = str(payload.get("adaptation_spell_ability", "")).strip()
        if adaptation_spell_ability not in ("int", "wis", "cha"):
            raise ValueError("「侏儒」自然适应需要选择次级幻影的施法属性（智力 / 感知 / 魅力）")

    # ---- 属性：按所选方式校验（BFRD：血统不提供属性加值）----
    method = str(payload.get("method", "pointbuy"))
    raw_base = payload.get("base") or {}
    base = {a: int(raw_base.get(a, 10)) for a in ABILITIES}
    _validate_abilities(method, base)

    abilities = {a: base[a] + race["bonuses"].get(a, 0) for a in ABILITIES}
    mods = {a: modifier(v) for a, v in abilities.items()}

    # ---- 技能：职业选 N 项 + 背景选 2 项（BFRD 模型，各自列表内、互不重复）；人类「雄心」另送 1 项 ----
    class_picks, bg_picks = _validate_skills(
        klass, background, payload.get("skills") or [], payload.get("background_skills") or []
    )
    all_skills = srd.skills()
    human_skill = str(payload.get("human_skill", "")).strip()
    if human_skill:
        if race_id != "human":
            raise ValueError("只有人类的「雄心」可以选择额外技能")
        if human_skill not in all_skills:
            raise ValueError(f"未知技能：{human_skill}")
        if human_skill in class_picks or human_skill in bg_picks:
            raise ValueError("额外技能与已选技能重复，请另选")
    prof = int(klass["prof_bonus"])
    skills = {}
    for skill_id in sorted(set(class_picks) | set(bg_picks) | ({human_skill} if human_skill else set())):
        skills[skill_id] = mods[all_skills[skill_id]["ability"]] + prof

    # ---- 天赋（BFRD）：背景三选一 + 可选自由天赋（如人类「雄心」）----
    talents = []
    bg_talent = str(payload.get("background_talent", "")).strip()
    if bg_talent:
        if bg_talent not in (background.get("talent_choices") or []):
            raise ValueError("背景天赋不在可选列表内")
        talents.append(bg_talent)
    free_talent = str(payload.get("talent", "")).strip()
    if free_talent:
        if free_talent not in srd.talents():
            raise ValueError("未知天赋")
        talents.append(free_talent)
    talent_schools = {}
    if "school-specialization" in talents:
        free_school = str(payload.get("talent_school", "")).strip()
        if free_school not in SPELL_SCHOOLS:
            raise ValueError("「学派专精」需要选择八大魔法学派之一")
        talent_schools["school-specialization"] = free_school

    # ---- 初始装备（BFRD：选项组由向导逐行选择；旧客户端无 choices 时退回文本行）----
    equipment_choices = payload.get("equipment_choices")
    if equipment_choices is not None:
        equipment_items, equipment = resolve_equipment(class_id, equipment_choices)
    else:
        equipment = [str(line) for line in (klass.get("equipment_lines") or [])]
        equipment_items = equipment_to_items(equipment)

    # ---- 穿着与 AC：显式指定（armor/worn_armor）优先；否则从装备选择推导（首个护甲 + 是否含盾）----
    explicit_armor = payload.get("worn_armor") or payload.get("armor")
    if explicit_armor:
        armor_key = resolve_armor_key(explicit_armor)
        if armor_key == "none" and str(explicit_armor).strip() not in ("", "none"):
            raise ValueError("未知护甲类型")
        shield = bool(payload.get("shield", payload.get("worn_shield", False)))
    else:
        armor_key, shield = "none", False
        for item in equipment_items:
            db = srd.items().get(item.get("id") or "") if item.get("id") else None
            if not db:
                continue
            if db.get("is_shield"):
                shield = True
            elif armor_key == "none" and db.get("category") == "Armor" and db.get("ac_base") is not None:
                armor_key = item["id"]
    armor_label = "无甲" if armor_key == "none" else armor_items()[armor_key]["label"]
    numbers = talent_numbers({
        "talents": talents, "worn_armor": armor_key,
        "talent_schools": talent_schools, "adaptation": adaptation,
    })
    ac = ac_value(mods["dex"], armor_key, shield, numbers)

    # ---- 其余派生数值（含数值型天赋效果）----
    hp_max = klass["hit_die"] + mods["con"] + numbers["hp_per_level"]     # 1 级 HP + 天赋每级加成
    saves = {a: mods[a] + (prof if a in klass["saves"] else 0) for a in ABILITIES}
    passive = 10 + mods["wis"] + (prof if "perception" in skills else 0) + numbers["passive_bonus"]
    speed = int(race["speed"]) + numbers["speed_bonus"]

    # 施法者才有：法术豁免 DC 与法术攻击加值（字段名 spellcasting，避免与 spells 混淆）
    spellcasting = None
    if klass.get("spell_ability"):
        sa = klass["spell_ability"]
        spellcasting = {"ability": sa, "save_dc": 8 + prof + mods[sa], "attack_bonus": prof + mods[sa]}

    # ---- 法术选择（BFRD 分层：戏法 / 已知或准备 / 法术书 / 仪式；兼容旧 payload）----
    known_spells = []
    spell_slots = {}
    spell_mode = None
    spell_buckets = {"cantrips": [], "known": [], "prepared": [], "spellbook": [], "rituals": []}
    spell_counters = {"spellbook_credits": 0, "swap_credits": 0}
    spell_model = 2 if klass.get("spell_ability") else None
    if klass.get("spell_ability"):
        data = srd.spells()
        spell_mode = str(payload.get("spell_mode", "class"))
        if spell_mode not in ("class", "free"):
            raise ValueError("未知法术选择模式")
        track = spells_mod.caster_track(class_id)
        limits = spells_mod.limits_for(class_id, 1, {"mods": mods})
        new_style = ("cantrips" in payload) or ("rituals" in payload)

        def _entry_of(sid):
            entry = data.get(sid)
            if not entry:
                raise ValueError(f"未知法术：{sid}")
            return entry

        def _check_source_circle(sid, entry, max_circle=None):
            if spell_mode != "free":
                sources = set(klass.get("sources") or [])
                spell_sources = set(entry.get("sources") or [])
                if sources and spell_sources and not (sources & spell_sources):
                    raise ValueError(f"「{entry['name']}」不在{class_id}的法术来源列表内")
            circle = int(entry.get("level", 0))
            cap_circle = limits["max_spell_level"] if max_circle is None else max_circle
            if circle > cap_circle:
                raise ValueError(f"建卡阶段不能选择 {circle} 环法术（最高 {cap_circle} 环）")

        if new_style:
            cantrip_picks = [str(x) for x in payload.get("cantrips") or []]
            main_picks = [str(x) for x in payload.get("spells") or []]
            ritual_picks = [str(x) for x in payload.get("rituals") or []]
            all_picks = cantrip_picks + main_picks + ritual_picks
            if len(all_picks) != len(set(all_picks)):
                raise ValueError("法术不能重复选择（同一法术只能放在一个栏位）")
            # 戏法
            if len(cantrip_picks) > limits["cantrips"]:
                raise ValueError(f"戏法最多选择 {limits['cantrips']} 个")
            for sid in cantrip_picks:
                entry = _entry_of(sid)
                if int(entry.get("level", 0)) != 0:
                    raise ValueError(f"「{entry['name']}」不是戏法")
                _check_source_circle(sid, entry, max_circle=0)
                spell_buckets["cantrips"].append({"id": sid, "name": entry["name"], "level": 0})
            # 主列表（按职业结构落位）
            if track["spellbook"]:
                if len(main_picks) > 6:
                    raise ValueError("法师初始法术书最多 6 条 1 环法术")
                for sid in main_picks:
                    entry = _entry_of(sid)
                    if int(entry.get("level", 0)) != 1:
                        raise ValueError("法师初始法术书只能放 1 环法术")
                    _check_source_circle(sid, entry)
                    spell_buckets["spellbook"].append({"id": sid, "name": entry["name"], "level": 1})
                spell_counters["spellbook_credits"] = 6 - len(main_picks)
                ordered = sorted(spell_buckets["spellbook"], key=lambda e: (e["level"], e["name"]))
                spell_buckets["prepared"] = [dict(e) for e in ordered[: limits["prepared"]]]
            elif track["known"]:
                if len(main_picks) > limits["known"]:
                    raise ValueError(f"已知法术最多选择 {limits['known']} 个")
                for sid in main_picks:
                    entry = _entry_of(sid)
                    if int(entry.get("level", 0)) == 0:
                        raise ValueError("戏法请放在戏法栏位")
                    _check_source_circle(sid, entry)
                    spell_buckets["known"].append(
                        {"id": sid, "name": entry["name"], "level": int(entry["level"])})
            else:
                if len(main_picks) > limits["prepared"]:
                    raise ValueError(f"准备法术最多选择 {limits['prepared']} 个")
                for sid in main_picks:
                    entry = _entry_of(sid)
                    if int(entry.get("level", 0)) == 0:
                        raise ValueError("戏法请放在戏法栏位")
                    _check_source_circle(sid, entry)
                    spell_buckets["prepared"].append(
                        {"id": sid, "name": entry["name"], "level": int(entry["level"])})
            # 仪式
            if ritual_picks and not track["rituals"]:
                raise ValueError("该职业没有仪式栏位")
            if len(ritual_picks) > limits["rituals"]:
                raise ValueError(f"仪式最多选择 {limits['rituals']} 个")
            for sid in ritual_picks:
                entry = _entry_of(sid)
                if not entry.get("ritual"):
                    raise ValueError(f"「{entry['name']}」不是仪式法术")
                _check_source_circle(sid, entry)
                spell_buckets["rituals"].append(
                    {"id": sid, "name": entry["name"], "level": int(entry["level"])})
        else:
            # 旧 payload（仅 spells/spell_mode）：按职业结构映射到分层模型
            picks = [str(s) for s in (payload.get("spells") or [])]
            errors = spells_mod.validate_spell_picks(picks, class_id, 1, spell_mode)
            if errors:
                raise ValueError("；".join(errors))
            state = spells_mod.build_spell_state(picks, class_id, 1, spell_mode)
            for item in state["spells"]:
                rec = {"id": item["id"], "name": item["name"], "level": int(item.get("level", 0))}
                if rec["level"] == 0:
                    spell_buckets["cantrips"].append(rec)
                elif track["spellbook"]:
                    spell_buckets["spellbook"].append(rec)
                elif track["known"]:
                    spell_buckets["known"].append(rec)
                else:
                    spell_buckets["prepared"].append(rec)
            if track["spellbook"]:
                spell_counters["spellbook_credits"] = max(0, 6 - len(spell_buckets["spellbook"]))
                ordered = sorted(spell_buckets["spellbook"], key=lambda e: (e["level"], e["name"]))
                spell_buckets["prepared"] = [dict(e) for e in ordered[: limits["prepared"]]]
        spell_slots = spells_mod.slots_for(class_id, 1)
        slot_state = {}
        for index, count in enumerate(spell_slots):
            if count > 0:
                slot_state[str(index + 1)] = {"max": count, "used": 0}
        spell_slots = slot_state
    # 侏儒自然适应：种族戏法「次级幻影」（非施法职业也可持有与施放）
    if adaptation == "侏儒":
        if spell_model is None:
            spell_model = 2
        cantrip_rec = {
            "id": "minor-illusion",
            "name": srd.spells().get("minor-illusion", {}).get("name", "次级幻影"),
            "level": 0,
            "racial": True,
        }
        spell_buckets["cantrips"] = [c for c in spell_buckets["cantrips"] if c.get("id") != "minor-illusion"] + [cantrip_rec]
    if spell_model is not None:
        union_holder = {"spell_model": 2, **{k: spell_buckets[k] for k in spell_buckets}}
        spells_mod.rebuild_spell_union(union_holder)
        known_spells = union_holder["spells"]

    # ---- 汇总成角色卡（id/归属由 state.RoomState 落库时补上）----
    character = {
        "id": None,
        "name": name,
        "gender": gender,
        "pending_asi": 0,
        "race_id": race_id,
        "race": race["name"],
        "heritage_id": heritage_id,
        "heritage": heritage["name"] if heritage else "",
        "adaptation": adaptation,
        "adaptation_spell_ability": adaptation_spell_ability,
        "alt_speed": numbers["alt_speed"],
        "class_id": class_id,
        "klass": klass["name"],
        "hit_die": klass["hit_die"],
        "background_id": background_id,
        "background": background["name"],
        "level": 1,
        "prof_bonus": prof,
        "method": method,
        "base": base,
        "abilities": abilities,
        "mods": mods,
        "saves": saves,
        "skills": skills,
        "hp": {"max": hp_max, "cur": hp_max},
        "hp_max_base": hp_max,
        "ac": ac,
        "speed": speed,
        "armor": armor_label,
        "worn_armor": armor_key,
        "shield": shield,
        "worn_shield": shield,
        "equipment": equipment,
        # 背包（动态）：建卡装备入包；四级货币（pp/gp/sp/cp 枚数，10 进制兑换；含背景腰包金币）
        "items": equipment_items,
        "coins": _initial_coins(background),
        "talents": talents,
        "talent_schools": talent_schools,
        "luck": 0,
        "exhaustion": 0,
        "dying": False,
        "stable": False,
        "dead": False,
        "death_saves": {"successes": 0, "failures": 0},
        "stable_until": None,
        "speed_base": speed,
        "human_skill": human_skill,
        "spellcasting": spellcasting,
        "spells": known_spells,
        "spell_slots": spell_slots,
        "spell_mode": spell_mode,
        "spell_model": spell_model,
        "cantrips": spell_buckets["cantrips"],
        "known": spell_buckets["known"],
        "prepared": spell_buckets["prepared"],
        "rituals": spell_buckets["rituals"],
        "swap_credits": spell_counters["swap_credits"],
        "effects": [],
        "concentration": None,
        "passive_perception": passive,
        "backstory": str(payload.get("backstory", "")).strip()[:2000],
        "owner_sid": None,
        "owner_nick": None,
    }
    # 法术书是法师专属：其他职业不落库 spellbook / spellbook_credits（v0.6.28）
    track = spells_mod.caster_track(str(class_id)) if klass.get("spell_ability") else None
    if track and track.get("spellbook"):
        character["spellbook"] = spell_buckets["spellbook"]
        character["spellbook_credits"] = spell_counters["spellbook_credits"]
    return character


def build_companion_payload(klass_id, race_id="human"):
    """
    为同伴自动构建一份"全推荐选择"的合法建卡 payload（AI 招募与管理页共用）：
    标准数组按职业优先级分配、背景默认士兵、技能各自列表内取前 N 项、
    天赋取背景第一项、施法职业附推荐法术包。返回的 payload 可直接交给 compute_character。
    """
    klass = srd.klass(klass_id)
    race_data = srd.race(race_id)    # 校验血统存在（并用于自然适应自动选择）
    backgrounds = srd.backgrounds()
    background_id = "soldier" if "soldier" in backgrounds else next(iter(backgrounds))
    background = backgrounds[background_id]
    allowed = klass["skill_choices"]
    if allowed == "any":
        allowed = list(srd.skills().keys())
    class_picks = list(allowed)[: int(klass["skill_count"])]
    bg_choices = list(background.get("skill_choices") or [])
    bg_picks = [sid for sid in bg_choices if sid not in class_picks][: int(background.get("skill_count", 2))]
    # 标准数组按职业推荐优先级逐项分配（六项铺满，主属性优先拿高值）
    base = {}
    order = list(klass["primary"]) + [a for a in ABILITIES if a not in klass["primary"]]
    for index, ability in enumerate(order):
        base[ability] = STANDARD_ARRAY[index % len(STANDARD_ARRAY)]
    payload = {
        "name": "同伴",
        "race": race_id,
        "adaptation": (race_data.get("adaptations") or [{}])[0].get("name", "") if race_data.get("adaptations") else "",
        "adaptation_spell_ability": "cha" if any(a.get("name") == "侏儒" for a in race_data.get("adaptations") or []) else "",
        "klass": klass_id,
        "background": background_id,
        "method": "standard",
        "base": base,
        "skills": class_picks,
        "background_skills": bg_picks,
        "background_talent": (background.get("talent_choices") or [None])[0],
        "armor": klass.get("default_armor") or "none",
        "shield": bool(klass.get("default_shield")),
        "backstory": "",
    }
    if race_id == "human":
        # 人类「雄心」：自选一项技能与一项天赋（同伴取"尚未选中的第一项"）
        spare = [sid for sid in srd.skills() if sid not in class_picks and sid not in bg_picks]
        if spare:
            payload["human_skill"] = spare[0]
        payload["talent"] = next(iter(srd.talents()), "")
    if klass.get("spell_ability"):
        payload["spells"] = spells_mod.recommended_picks(klass_id, 1, "class")
        payload["spell_mode"] = "class"
    # 装备选项组：每行取第一选项，武器选择位按类别/近战要求取排序第一把
    weapons = mundane_weapons()
    choices = []
    for spec in equipment_specs(klass_id):
        side = spec["choices"][0]
        picked = []
        for prompt in side["weapon_picks"]:
            pool = [
                wid for wid, w in sorted(weapons.items())
                if str(w.get("weapon_category_zh", "")).startswith(prompt["category"])
                and (not prompt["melee"] or _is_melee_weapon(w))
            ]
            picked.extend(pool[: prompt["count"]])
        choices.append({"index": spec["index"], "key": side["key"], "weapons": picked})
    payload["equipment_choices"] = choices
    return payload


def _validate_abilities(method, base):
    """按生成方式校验六项基础属性：点买（BFRD 32 点、8-18）/ 标准数组 / 掷骰。"""
    for value in base.values():
        if not 1 <= value <= 20:
            raise ValueError("属性值需在 1-20 之间")
    if method == "pointbuy":
        for value in base.values():
            if not 8 <= value <= 18:
                raise ValueError("点买属性需在 8-18 之间")
        cost = point_buy_cost(base)
        if cost > POINT_BUY_TOTAL:
            raise ValueError(f"点买共花费 {cost} 点，超过 {POINT_BUY_TOTAL} 点上限")
    elif method == "standard":
        if sorted(base.values()) != sorted(STANDARD_ARRAY):
            raise ValueError("标准数组必须恰好使用 " + "/".join(str(v) for v in STANDARD_ARRAY) + " 各一次")
    elif method == "roll":
        for value in base.values():
            if not 3 <= value <= 18:
                raise ValueError("掷骰属性需在 3-18 之间（含掷骰奖励加成）")
    else:
        raise ValueError("未知属性生成方式")


def _validate_skills(klass, background, class_picks, bg_picks):
    """
    校验技能选择（BFRD 模型：职业选 N 项 + 背景选 2 项，各自在列表内、互不重复）。
    """
    all_skills = srd.skills()

    def check_picks(picks, choices, count, who):
        picks = [str(s) for s in picks]
        if len(picks) != len(set(picks)):
            raise ValueError(f"{who}技能不能重复选择")
        if choices != "any":
            bad = [s for s in picks if s not in choices]
            if bad:
                raise ValueError(f"{who}不可选择这些技能")
        for skill_id in picks:
            if skill_id not in all_skills:
                raise ValueError(f"未知技能：{skill_id}")
        if len(picks) != int(count):
            raise ValueError(f"需要选择 {count} 项{who}技能，当前 {len(picks)} 项")
        return picks

    class_picks = check_picks(class_picks, klass["skill_choices"], klass["skill_count"], "职业")
    bg_picks = check_picks(bg_picks, background.get("skill_choices") or [], background.get("skill_count", 2), "背景")
    overlap = sorted(set(class_picks) & set(bg_picks))
    if overlap:
        raise ValueError("职业与背景所选的技能重复，请另选")
    return class_picks, bg_picks
