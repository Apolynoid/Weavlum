"""
房间动作层：所有"改变游戏状态"的操作在这里实现一次，三个入口共用——
  1. AI 工具调用（ai.py 的对话循环；AI 只是"会说人话的调用方"）
  2. 玩家 WS 消息（main.py 的具体消息类型，如掷骰/移动/休息）
  3. 人类 GM 控制台（main.py 的通用 action 消息 + 跑团页 GM 控制台 UI）

架构原则（用户裁定）：**系统/引擎是第一公民**，人类 GM 与 AI GM 同等权重。
之前这些操作只挂在 AI 工具分支上，导致人类 GM 有大量操作做不了——那是个错误。

每个动作返回统一结构：
  {
    "text":    给调用方（AI / GM 控制台）的确认文本,
    "notes":   需要写入共享日志并广播的系统事件文本列表,
    "changed": 状态是否变化（调用方据此广播快照）,
    "roll_record": 可选，掷骰动作的结构化记录（AI 的 rolls 通道用）,
    "attach_board": 可选，是否在工具结果后附带战术板简报（AI 上下文用）,
  }
读操作（查询队伍/怪物/战役）changed=False。
"""

import json
import re

from . import campaign as campaign_mod
from . import characters as characters_mod
from . import debuglog  # noqa: F401  （保留给后续动作级调试日志）
from . import engine, srd
from . import spells as spells_mod
from . import state as state_mod

ABIL_SHORT = {"str": "力", "dex": "敏", "con": "体", "int": "智", "wis": "感", "cha": "魅"}


# ---------------------------------------------------------------------------
# 渲染器（供动作与 AI 提示词共用；原先挂在 ai.py，迁到动作层以消除 AI 特权）
# ---------------------------------------------------------------------------


def board_brief(room):
    """战术板简报：当前回合 + 每只怪物到各角色的真实距离（战斗中才有内容）。"""
    combat = room.combat
    if not combat.get("active"):
        return ""
    combatants = combat.get("combatants") or []
    pcs = [c for c in combatants if c.get("kind") in ("pc", "ally") and c.get("pos")]
    monsters = [c for c in combatants if c.get("kind") == "monster" and c.get("pos")]
    if not monsters:
        return ""
    turn = combat.get("turn", 0)
    current = combatants[turn] if 0 <= turn < len(combatants) else None
    lines = []
    if current:
        lines.append(f"当前回合：{current['name']}（第 {combat.get('round', 1)} 轮）")
    for monster in monsters:
        dists = []
        for pc in pcs:
            dist = (
                (monster["pos"]["x"] - pc["pos"]["x"]) ** 2 + (monster["pos"]["y"] - pc["pos"]["y"]) ** 2
            ) ** 0.5
            dists.append(f"距{pc['name']} {dist:.0f} 尺")
        lines.append(f"{monster['name']}（{monster['id']}）：" + ("、".join(dists) if dists else "在场"))
    header = "【战术板】板面约 120×60 尺。近战需在 5 尺内；移动与射程叙述必须遵守这些实际距离和各自动速度。任何棋子的移动——玩家声明「向某目标移动 N 尺」或怪物接近/换目标——都必须调用 move_combatant_toward 同步棋子，不要只在叙述里移动。当一名行动者结束行动、轮到下一位时，调用 advance_turn 推进先攻条。"
    return header + "\n" + "\n".join(lines)


def gods_brief():
    """神祇速查（07/05 章数据）：各神系的一行式神祇清单（名/称号/领域/圣徽），供 AI 引用。"""
    lines = []
    for pantheon in srd.gods().values():
        deities = "；".join(
            f"{d['name']}（{d.get('title') or '—'}｜领域：{'、'.join(d.get('domains') or [])}；圣徽：{d.get('symbol') or '—'}）"
            for d in pantheon.get("deities") or []
        )
        lines.append(f"{pantheon['name']}：{deities}")
    return "\n".join(lines)


def conditions_brief():
    """状态速查（附录 16 条）：名称 + 效果全文（压平换行），供 AI 裁定状态用。"""
    lines = []
    for entry in srd.conditions().values():
        desc = " ".join((entry.get("desc_zh") or "").split())
        lines.append(f"{entry.get('name', '')}：{desc}")
    return "\n".join(lines)


def party_features_brief(room):
    """
    队伍规则资料：每位上桌角色的职业特性/子职业特性（到当前等级为止）、
    血统与传承特性文本——让 AI 按 Black Flag 原文运行特性，而不是凭通用知识猜。
    """
    lines = []
    for char in room.characters.values():
        if not (char.get("owner_sid") or char.get("companion")):
            continue
        level = int(char.get("level") or 1)
        try:
            klass = srd.klass(char.get("class_id") or "")
        except ValueError:
            klass = {}
        head = f"- {char.get('name', '')}（{char.get('klass', '')} {level} 级"
        if char.get("subclass_name"):
            head += f"，{char['subclass_name']}"
        head += "）"
        lines.append(head)
        feats = [
            f for f in (klass.get("features") or {}).values()
            if int(f.get("level") or 0) <= level and f.get("desc")
        ]
        feats.sort(key=lambda f: int(f.get("level") or 0))
        for f in feats:
            desc = " ".join(str(f["desc"]).split())
            lines.append(f"  [{f.get('level')}级·{f.get('name')}] {desc[:300]}")
        sub = (klass.get("subclasses") or {}).get(str(char.get("subclass") or ""))
        if sub:
            sub_feats = [
                f for f in (sub.get("features") or {}).values()
                if int(f.get("level") or 0) <= level and f.get("desc")
            ]
            sub_feats.sort(key=lambda f: int(f.get("level") or 0))
            for f in sub_feats:
                desc = " ".join(str(f["desc"]).split())
                lines.append(f"  [{f.get('level')}级·{sub.get('name', '')}·{f.get('name')}] {desc[:300]}")
        race = srd.races().get(str(char.get("race_id") or ""))
        if race and race.get("traits_desc"):
            adapt_note = f"（自然适应：{char['adaptation']}）" if char.get("adaptation") else ""
            lines.append(f"  [血统·{race.get('name', '')}{adapt_note}] " + " ".join(str(race["traits_desc"]).split())[:400])
        heritage = srd.heritages().get(str(char.get("heritage_id") or ""))
        if heritage and heritage.get("traits_desc"):
            lines.append(f"  [传承·{heritage.get('name', '')}] " + " ".join(str(heritage["traits_desc"]).split())[:300])
        # 下一级预告（职业进阶表）：便于 AI 回答"升级能拿什么"，也用于里程碑提示
        prog = klass.get("progression") or {}
        rows = prog.get("rows") or {}
        nxt = str(level + 1)
        if nxt in rows:
            row, cur = rows[nxt], rows.get(str(level), {})
            bits = []
            feats_next = row.get("features") or []
            if feats_next:
                bits.append("特性 " + "、".join(feats_next))
            for key, label in (("pb", "熟练"), ("cantrips", "戏法"), ("spells", "已知法术"), ("rituals", "仪式")):
                a, b = cur.get(key), row.get(key)
                if b is not None and a != b:
                    bits.append(f"{label} {a if a is not None else '—'}→{b}")
            slot_deltas = []
            for ring in range(1, 10):
                a, b = int(cur.get(f"slot_{ring}") or 0), int(row.get(f"slot_{ring}") or 0)
                if a != b:
                    slot_deltas.append(f"{ring}环 {a}→{b}")
            if slot_deltas:
                bits.append("法术位 " + "、".join(slot_deltas))
            for col in prog.get("columns") or []:
                key, label = col.get("key", ""), col.get("label", "")
                if not key or key in ("level", "pb", "features", "cantrips", "spells", "rituals", "prepared") or key.startswith("slot_"):
                    continue
                a, b = cur.get(key), row.get(key)
                if a != b and not isinstance(a, list) and not isinstance(b, list):
                    bits.append(f"{label} {a if a not in (None, '') else '—'}→{b if b not in (None, '') else '—'}")
            if bits:
                lines.append(f"  下一级（{nxt} 级）将获得：" + "；".join(bits))
    return "\n".join(lines)


def class_features_payload():
    """
    职业/子职业特性文本打包（角色卡与建卡页悬浮说明用）：
      {class_id: {"features": [{"name","level","desc"}...],
                  "subclasses": {sid: {"name", "features": [...]}}}}
    """
    out = {}
    for cid, klass in srd.classes().items():
        feats = []
        for f in (klass.get("features") or {}).values():
            feats.append({"name": f.get("name", ""), "level": int(f.get("level") or 0), "desc": f.get("desc") or ""})
        feats.sort(key=lambda f: f["level"])
        subs = {}
        for sid, sub in (klass.get("subclasses") or {}).items():
            sub_feats = []
            for f in (sub.get("features") or {}).values():
                sub_feats.append({"name": f.get("name", ""), "level": int(f.get("level") or 0), "desc": f.get("desc") or ""})
            sub_feats.sort(key=lambda f: f["level"])
            subs[sid] = {"name": sub.get("name", sid), "name_en": sub.get("name_en", ""),
                         "desc": sub.get("desc", ""), "features": sub_feats}
        out[cid] = {"features": feats, "subclasses": subs}
    return out


def party_brief(room):
    """队伍简报：属性修正/熟练/法术位/效果等引擎实时数据（AI 只能引用，不能编造）。"""
    lines = []
    skill_names = {sid: info["name"] for sid, info in srd.skills().items()}
    for char in room.characters.values():
        owner = char.get("owner_nick")
        if owner:
            who = f"玩家：{owner}"
        elif char.get("companion"):
            who = "NPC 同伴（在场，由你控制其行动）"
        else:
            who = "未入场（不在场，不要让他出现在剧情或战斗中）"
        gender = char.get("gender") or ""
        head = (
            f"- {char['name']}（{gender + ' ' if gender else ''}{char['race']}{char['klass']} {char['level']}级，{who}）"
            f" HP {char['hp']['cur']}/{char['hp']['max']}，AC {char['ac']}，速度 {char['speed']}尺"
        )
        mods = char.get("mods", {})
        prof = char.get("prof_bonus", 2)
        parts = [" ".join(f"{ABIL_SHORT[k]}{mods[k]:+d}" for k in ABIL_SHORT if k in mods)]
        save_prof = [
            f"{ABIL_SHORT[k]}{v:+d}"
            for k, v in char.get("saves", {}).items()
            if v - mods.get(k, 0) == prof
        ]
        if save_prof:
            parts.append("熟练豁免 " + " ".join(save_prof))
        skills = [f"{skill_names.get(sid, sid)}{v:+d}" for sid, v in char.get("skills", {}).items()]
        if skills:
            parts.append("熟练技能 " + "、".join(skills))
        parts.append(f"被动察觉 {char.get('passive_perception', 10)}")
        if char.get("spellcasting"):
            parts.append(f"施法 DC {char['spellcasting']['save_dc']} 攻击 {char['spellcasting']['attack_bonus']:+d}")
        slots = char.get("spell_slots") or {}
        if slots:
            slot_txt = "、".join(
                f"{key}环 {value.get('max', 0) - value.get('used', 0)}/{value.get('max', 0)}"
                for key, value in sorted(slots.items())
            )
            parts.append("法术位 " + slot_txt)
        known = char.get("spells") or []
        if known and int(char.get("spell_model") or 1) >= 2 and char.get("spellcasting"):
            limits = spells_mod.limits_for(char.get("class_id"), char.get("level", 1), char)
            buckets = []
            buckets.append(f"戏法 {len(char.get('cantrips') or [])}/{limits['cantrips']}")
            if (char.get("class_id") or "") in spells_mod.KNOWN_TRACK_CLASSES:
                buckets.append(f"已知 {len(char.get('known') or [])}/{limits['known']}")
            else:
                buckets.append(f"准备 {len(char.get('prepared') or [])}/{limits['prepared']}")
            if char.get("spellbook"):
                buckets.append(f"法术书 {len(char['spellbook'])}")
            if (char.get("class_id") or "") in ("bard", "cleric", "druid", "wizard"):
                buckets.append(f"仪式 {len(char.get('rituals') or [])}/{limits['rituals']}")
            parts.append("法术 " + "、".join(buckets))
            listed = char.get("known") if (char.get("class_id") or "") in spells_mod.KNOWN_TRACK_CLASSES else char.get("prepared")
            if listed:
                parts.append("可施放 " + "、".join(
                    e.get("name", "") + ("" if e.get("level") == 0 else f"({e['level']}环)") for e in listed
                ))
        elif known:
            known_txt = "、".join(
                s.get("name", "") + ("" if s.get("level") == 0 else f"({s.get('level')}环)") for s in known
            )
            parts.append("已知法术 " + known_txt)
        if char.get("concentration"):
            parts.append(f"专注：「{char['concentration']}」")
        life_key, life_label = characters_mod.life_state(char)
        if life_key == "dying":
            saves = char.get("death_saves") or {}
            parts.append(f"【濒死·昏迷】死亡豁免 {saves.get('successes', 0)} 成功 / {saves.get('failures', 0)} 失败（回合开始引擎自动掷）")
        elif life_key == "stable":
            parts.append("【稳定·昏迷】0 HP，无需死亡豁免，等待 1d4 小时自动苏醒或治疗")
        elif life_key == "dead":
            parts.append("【已死亡】普通治疗无效，需要复苏术/死者复活/复活术/真实复活术")
        exhaustion = int(char.get("exhaustion") or 0)
        if exhaustion > 0:
            parts.append(f"力竭 {exhaustion} 级（1检定劣势/2速度减半/3攻击豁免劣势/4HP上限减半/5速度0/6死亡）")
        level_n = int(char.get("level") or 1)
        hd_used = int(char.get("hit_dice_used") or 0)
        parts.append(f"生命骰 {level_n - hd_used}/{level_n}（d{int(char.get('hit_die') or 8)}，短休可消耗回血）")
        contacts = int(char.get("contacts") or 0)
        clues = int(char.get("clues") or 0)
        if contacts or clues:
            parts.append(f"人脉 {contacts}、线索 {clues}")
        luck = int(char.get("luck") or 0)
        if luck:
            parts.append(f"幸运 {luck}/5")
        talents = char.get("talents") or []
        if talents:
            talent_names = {tid: t.get("name", tid) for tid, t in srd.talents().items()}
            school = (char.get("talent_schools") or {}).get("school-specialization")
            shown = "、".join(talent_names.get(tid, tid) + (f"（{school}）" if tid == "school-specialization" and school else "") for tid in talents)
            parts.append("天赋 " + shown)
        active_effects = [
            e.get("name", "") + f"（{spells_mod.effect_status(e, room.clock_minutes)}）"
            for e in (char.get("effects") or [])
        ]
        if active_effects:
            parts.append("效果 " + "、".join(active_effects))
        lines.append(head + "\n  " + "；".join(parts))
    return "\n".join(lines) if lines else "-（暂无角色）"


def monster_lookup_text(args):
    """按名字/CR 区间过滤怪物图鉴，返回给调用方看的纯文本列表（含可引用的 id 与特性简报）。"""

    def _cr(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    name = str(args.get("name") or "").strip().lower()
    max_cr = _cr(args.get("max_cr"))
    min_cr = _cr(args.get("min_cr"))
    matches = []
    for mid, monster in srd.monsters().items():
        cr = float(monster["cr"])
        if max_cr is not None and cr > max_cr:
            continue
        if min_cr is not None and cr < min_cr:
            continue
        if name and name not in mid.lower() and name not in monster["name"]:
            continue
        matches.append((mid, monster))
    matches.sort(key=lambda item: (float(item[1]["cr"]), item[0]))
    if not matches:
        return "没有找到匹配的怪物。请换关键词或放宽 CR 区间。"
    lines = []
    for mid, monster in matches[:15]:
        attack = monster.get("attack") or {}
        attack_txt = f"{attack.get('name', '攻击')} +{attack.get('to_hit', 0)}，伤害 {attack.get('damage', '')}"
        hints = [str(item.get("name", "")) for item in (monster.get("abilities_zh") or [])[:2] if item.get("name")]
        hint_txt = ("｜特性：" + ("、".join(hints))[:40]) if hints else ""
        lines.append(
            f"- id={mid} {monster['name']}（CR {monster.get('cr_label', monster['cr'])}，XP {monster['xp']}）"
            f"AC {monster['ac']}，HP {monster['hp']}，速度 {monster['speed']}尺，先攻{monster.get('init_bonus', 0):+d}，攻击：{attack_txt}{hint_txt}"
        )
    return "以下为引擎数据（必须直接采用）：\n" + "\n".join(lines)


def spawn_monsters_text(room, args):
    """解析生成参数并调用引擎生成怪物，返回中文结果（含编号名与先攻）。"""
    listed = args.get("list")
    if not isinstance(listed, list):
        listed = []
    specs = []
    unknown = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id", "")).strip()
        monster = srd.monsters().get(mid)
        if not monster:
            if mid:
                unknown.append(mid)
            continue
        try:
            count = int(item.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, 12))
        specs.append((monster, count))
    if not specs:
        return "没有生成任何怪物（id 无效）。请先用 query_monsters 查询有效 id。"
    added = state_mod.spawn_monsters(room, specs)
    text = "已加入先攻条：" + "、".join(
        f"{c['name']}（id={c['id']}，先攻 {c['init']}，HP {c['hp']}，AC {c['ac']}）" for c in added
    )
    if unknown:
        text += "。未能识别的 id：" + "、".join(unknown)
    return text


def _result(text, notes=None, changed=False, roll_record=None, attach_board=False):
    """动作返回结构统一构造器。"""
    return {
        "text": text,
        "notes": list(notes or []),
        "changed": changed,
        "roll_record": roll_record,
        "attach_board": attach_board,
    }


# ---------------------------------------------------------------------------
# 读操作
# ---------------------------------------------------------------------------


def _get_party(room, args):
    return _result(party_brief(room))


def _get_campaign(room, args):
    if not room.campaign:
        return _result("尚未设置战役结构（主机需在管理页生成并保存）")
    return _result(campaign_mod.render_brief(room.campaign))


def _query_monsters(room, args):
    return _result(monster_lookup_text(args))


def _roll_dice(room, args):
    """掷骰（结构化记录经 roll_record 返回；AI 与 GM 控制台共用同一实现）。"""
    expression = str(args.get("expression", "1d20"))
    reason = str(args.get("reason", "掷骰"))[:60]
    dc = args.get("dc")
    dc = int(dc) if dc is not None else None
    mode = str(args.get("mode") or "normal")
    simple = re.fullmatch(r"1d20([+-]\d+)?", expression.strip().replace(" ", ""))
    if mode in ("advantage", "disadvantage") and simple:
        modifier = int(simple.group(1) or 0)
        check = engine.roll_check(modifier=modifier, dc=dc, mode=mode, label=reason)
        record = {
            "who": "AI GM",
            "reason": reason,
            "expr": expression,
            "total": check["total"],
            "breakdown": check["breakdown"],
            "dc": dc,
            "success": check["success"],
            "mode": mode,
        }
    else:
        result = engine.roll_expr(expression)
        success = (result["total"] >= dc) if dc is not None else None
        record = {
            "who": "AI GM",
            "reason": reason,
            "expr": result["expr"],
            "total": result["total"],
            "breakdown": result["breakdown"],
            "dc": dc,
            "success": success,
        }
    # 失败积累幸运（BFRD：攻击未命中/豁免失败获得 1 点；调用方传 character 时自动结算）
    luck_text = ""
    if args.get("character") and record.get("success") is False:
        char = state_mod.find_character(room, args.get("character"))
        if char:
            d4 = engine.roll_expr("1d4")["total"] if int(char.get("luck") or 0) >= 5 else None
            _, luck_note = characters_mod.luck_add(char, 1, d4_roll=d4, from_failure=True)
            room.persist_character(char)
            luck_text = f"；{char.get('name', '')} {luck_note}"
    return _result(json.dumps(record, ensure_ascii=False) + luck_text, roll_record=record)


# ---------------------------------------------------------------------------
# 战斗动作
# ---------------------------------------------------------------------------


def _spawn_monsters(room, args):
    return _result(spawn_monsters_text(room, args), changed=True, attach_board=True)


def _move_combatant_toward(room, args):
    moved, summary = state_mod.move_combatant_toward(
        room, args.get("mover", ""), args.get("target", ""), args.get("feet", 30) or 30
    )
    return _result(summary, notes=[summary] if moved else [], changed=True, attach_board=True)


def _advance_turn(room, args):
    expired = state_mod.next_turn(room)
    combat = room.combat
    combatants = combat.get("combatants") or []
    turn = combat.get("turn", 0)
    current = combatants[turn] if 0 <= turn < len(combatants) else None
    text = (
        f"先攻条已推进：第 {combat.get('round', 1)} 轮，当前行动者："
        f"{current['name'] if current else '—'}"
    )
    if expired:
        text += "；" + "；".join(expired)
    return _result(text, notes=expired, changed=True, attach_board=True)


def _adjust_hp(room, args):
    summary = state_mod.adjust_hp(room, args.get("target", ""), args.get("delta", 0))
    return _result(summary, notes=[summary], changed=True, attach_board=True)


def _end_combat(room, args):
    text = state_mod.end_combat(room, str(args.get("reason", "") or "剧情结束"))
    return _result(text, notes=[text], changed=True)


# ---------------------------------------------------------------------------
# 物品与背包动作（完整版：内置物品库 + 金币 + 交易）
# ---------------------------------------------------------------------------


def _money_text(cp):
    """铜币数 → 中文金额文本（X 金 Y 银 Z 铜）。"""
    total = int(round(cp))
    if total <= 0:
        return "0 铜"
    gp, rem = divmod(total, 100)
    sp, copper = divmod(rem, 10)
    parts = []
    if gp:
        parts.append(f"{gp} 金")
    if sp:
        parts.append(f"{sp} 银")
    if copper:
        parts.append(f"{copper} 铜")
    return "".join(parts)


def _resolve_item(query):
    """物品库解析：id 或中文名 → (id, 条目)；查不到返回 (None, None)。"""
    items = srd.items()
    key = str(query or "").strip()
    if key in items:
        return key, items[key]
    for idx, entry in items.items():
        if entry.get("name_zh") == key:
            return idx, entry
    return None, None


def _find_inventory_item(char, query):
    """在角色背包里按 id / 全名 / 子串查条目；找不到返回 None。"""
    key = str(query or "").strip()
    if not key:
        return None
    for item in char.get("items") or []:
        if key == item.get("id") or key == item.get("name") or key in str(item.get("name", "")):
            return item
    return None


def _normalize_qty(value, default=1):
    try:
        qty = int(value or default)
    except (TypeError, ValueError):
        qty = default
    return max(1, min(qty, 999))


def _insert_item(char, item_id, name, qty, note=""):
    """把物品放入背包（同 id/自由文本名 + 同备注自动合并数量；备注不同则分开存放）。"""
    note = str(note or "").strip()[:200]
    items = char.setdefault("items", [])
    for item in items:
        same_key = (item_id and item.get("id") == item_id) or (
            not item_id and not item.get("id") and item.get("name") == name)
        if same_key and str(item.get("note") or "").strip() == note:
            item["qty"] = int(item.get("qty", 1)) + qty
            return
    entry = {"id": item_id, "name": name, "qty": qty}
    if note:
        entry["note"] = note
    items.append(entry)


def inventory_text(char):
    """背包文本视图（供 AI/控制台查询）：资产（四级硬币）、负重与物品。"""
    lines = [f"{char.get('name', '')} · 资产 {characters_mod.coins_text(char.get('coins'))}（总值 {_money_text(characters_mod.coin_total_cp(char.get('coins')))}）"]
    carry = characters_mod.carry_info(char)
    carry_line = f"负重 {carry['weight']} / {carry['capacity']} 磅（{carry['label']}"
    carry_line += f"：{carry['hint']}）" if carry["hint"] else "）"
    if carry["unknown_count"]:
        carry_line += f"，另有 {carry['unknown_count']} 件自定义物品未计重"
    lines.append(carry_line)
    items = char.get("items") or []
    if items:
        lines.append("背包：" + "、".join(
            f"{it.get('name')}×{it.get('qty', 1)}" + (f"（{it['note']}）" if it.get("note") else "")
            for it in items
        ))
    else:
        lines.append("背包：空")
    return "\n".join(lines)


def _reward_party(room, args):
    """
    全队发奖（GM 控制台 / 剧情结算用）：向每位上桌角色各发放同一物品与/或金额。
    物品与金额至少给其一；逐人走 add_item / adjust_gold 并汇总播报。
    """
    seated = [c for c in room.characters.values() if c.get("owner_sid")]
    if not seated:
        raise ValueError("队伍里还没有上桌角色（无人选角入场）")
    item_query = str(args.get("item") or "").strip()
    qty = _normalize_qty(args.get("qty"))
    try:
        gold = round(float(args.get("gold") or 0), 2)
    except (TypeError, ValueError):
        raise ValueError("金额不合法")
    if not item_query and not gold:
        raise ValueError("请指定要发放的物品或金额")
    parts = []
    for char in seated:
        if item_query:
            parts.append(_add_item(room, {"character": char["id"], "item": item_query, "qty": qty})["text"])
        if gold:
            parts.append(_adjust_gold(room, {"character": char["id"], "delta": gold})["text"])
    return _result("；".join(parts), notes=parts, changed=True)


def _get_inventory(room, args):
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    return _result(inventory_text(char), changed=False)


def _add_item(room, args):
    """入库物品：命中物品库带 id 与规范名；自定义物品按自由文本名。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    qty = _normalize_qty(args.get("qty"))
    item_id, entry = _resolve_item(args.get("item", ""))
    if entry:
        name = entry.get("name_zh") or item_id
    else:
        name = str(args.get("item", "")).strip()
        if not name:
            raise ValueError("请提供物品 id 或名称")
    note = str(args.get("note") or "").strip()[:200]
    _insert_item(char, item_id, name, qty, note)
    room.persist_character(char)
    text = f"{char['name']} 获得 {name}×{qty}" + (f"（{note}）" if note else "")
    return _result(text, notes=[text], changed=True)


def _remove_item(room, args):
    """移出物品（消耗/丢弃/被偷）；数量不足时报错。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    qty = _normalize_qty(args.get("qty"))
    item = _find_inventory_item(char, args.get("item", ""))
    if not item:
        raise ValueError(f"背包里没有：{args.get('item', '')}")
    have = int(item.get("qty", 1))
    if qty > have:
        raise ValueError(f"{item.get('name')} 只有 {have} 个")
    item["qty"] = have - qty
    if item["qty"] <= 0:
        char["items"].remove(item)
    room.persist_character(char)
    text = f"{char['name']} 失去 {item.get('name')}×{qty}"
    return _result(text, notes=[text], changed=True)


def _adjust_gold(room, args):
    """
    资产增减（delta，单位金，可小数）或直接设值（value，单位金）：
    按 10 铜=1 银、10 银=1 金、10 金=1 铂 自动并档；扣减超过持有会报错。
    """
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    coins = char.get("coins") or {}
    if args.get("value") is not None:
        try:
            target_cp = int(round(float(args.get("value") or 0) * 100))
        except (TypeError, ValueError):
            raise ValueError("金币数值不合法")
        if target_cp < 0:
            raise ValueError("金币数值不能为负")
        delta_cp = target_cp - characters_mod.coin_total_cp(coins)
    else:
        try:
            delta_cp = int(round(float(args.get("delta") or 0) * 100))
        except (TypeError, ValueError):
            raise ValueError("金币增减值不合法")
    if delta_cp == 0:
        return _result(f"{char['name']} 的资产保持 {characters_mod.coins_text(coins)}", changed=False)
    char["coins"] = characters_mod.coins_change_cp(coins, delta_cp)
    room.persist_character(char)
    verb = "增加" if delta_cp > 0 else "减少"
    text = f"{char['name']} 的资产{verb} {_money_text(abs(delta_cp))}，现为 {characters_mod.coins_text(char['coins'])}"
    return _result(text, notes=[text], changed=True)


def _set_coins(room, args):
    """直接设置四级硬币枚数（信任制：角色卡四格编辑用）。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    raw = args.get("coins") or {}
    coins = {}
    for key in characters_mod.COIN_ORDER:
        try:
            value = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        coins[key] = max(0, min(value, 1000000))
    char["coins"] = coins
    room.persist_character(char)
    text = f"{char['name']} 的资产调整为 {characters_mod.coins_text(coins)}"
    return _result(text, notes=[text], changed=True)


def _exchange_currency(room, args):
    """
    四级硬币兑换（10 铜 = 1 银、10 银 = 1 金、10 金 = 1 铂；支持任意两档来回兑，金额必须整兑）。
    """
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    frm = str(args.get("from", "")).lower()
    to = str(args.get("to", "")).lower()
    rates = characters_mod.COIN_RATES_CP
    labels = characters_mod.COIN_LABELS
    if frm not in rates or to not in rates or frm == to:
        raise ValueError("兑换币种不合法（可选 cp/sp/gp/pp）")
    amount = _normalize_qty(args.get("amount"))
    coins = dict(char.get("coins") or {})
    have = int(coins.get(frm) or 0)
    if amount > have:
        raise ValueError(f"{labels[frm]}不足：只有 {have} 枚")
    value = amount * rates[frm]
    if value % rates[to] != 0:
        raise ValueError(f"{amount} {labels[frm]} 不能整兑为 {labels[to]}（请凑整：10 进制兑换）")
    coins[frm] = have - amount
    coins[to] = int(coins.get(to) or 0) + value // rates[to]
    char["coins"] = coins
    room.persist_character(char)
    text = f"{char['name']} 把 {amount} {labels[frm]} 兑换为 {value // rates[to]} {labels[to]}"
    return _result(text, notes=[text], changed=True)


def _buy_item(room, args):
    """按库价购买：钱不够或物品无定价时报错；付款自动找零（四级硬币）。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    qty = _normalize_qty(args.get("qty"))
    item_id, entry = _resolve_item(args.get("item", ""))
    if not entry:
        raise ValueError("购买请从物品库选择（支持 id 或中文名）")
    name = entry.get("name_zh") or item_id
    cost = entry.get("cost_cp")
    if cost is None:
        raise ValueError(f"「{name}」没有价格记录，无法自动购买（可用 adjust_gold 手动记账）")
    cost_cp = cost * qty
    try:
        char["coins"] = characters_mod.coins_pay_cp(char.get("coins"), cost_cp)
    except ValueError:
        raise ValueError(f"钱不够：需要 {_money_text(cost_cp)}，现有 {characters_mod.coins_text(char.get('coins'))}")
    note = str(args.get("note") or "").strip()[:200]
    _insert_item(char, item_id, name, qty, note)
    room.persist_character(char)
    text = f"{char['name']} 买入 {name}×{qty}，花费 {_money_text(cost_cp)}，余额 {characters_mod.coins_text(char['coins'])}"
    return _result(text, notes=[text], changed=True)


def _set_item_note(room, args):
    """
    给背包物品写/改/清备注（如治疗药水的品质、来历）。
    item 支持 id / 全名 / 子串；同名多条时可传 old_note 精确定位（UI 即如此传）。
    """
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    key = str(args.get("item", "")).strip()
    if not key:
        raise ValueError("请指定要备注的物品")
    note = str(args.get("note") or "").strip()[:200]
    candidates = [
        item for item in (char.get("items") or [])
        if key == item.get("id") or key == item.get("name") or key in str(item.get("name", ""))
    ]
    if not candidates:
        raise ValueError(f"背包里没有：{key}")
    if len(candidates) > 1 and "old_note" in args:
        old_note = str(args.get("old_note") or "").strip()
        candidates = [item for item in candidates if str(item.get("note") or "").strip() == old_note] or candidates[:1]
    item = candidates[0]
    if note:
        item["note"] = note
    else:
        item.pop("note", None)
    room.persist_character(char)
    text = f"{char['name']} 的「{item.get('name')}」备注" + (f"：{note}" if note else "已清除")
    return _result(text, notes=[text], changed=True)


def _sell_item(room, args):
    """出售背包物品：物品库定价的半价回收（铜价向下取整）；自定义物品不可自动定价。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    qty = _normalize_qty(args.get("qty"))
    item = _find_inventory_item(char, args.get("item", ""))
    if not item:
        raise ValueError(f"背包里没有：{args.get('item', '')}")
    entry = srd.items().get(item.get("id") or "")
    if not entry or entry.get("cost_cp") is None:
        raise ValueError("自定义物品没有定价，无法自动出售（可用 adjust_gold 手动记账）")
    have = int(item.get("qty", 1))
    if qty > have:
        raise ValueError(f"{item.get('name')} 只有 {have} 个")
    gain_cp = entry["cost_cp"] * qty // 2
    item["qty"] = have - qty
    if item["qty"] <= 0:
        char["items"].remove(item)
    char["coins"] = characters_mod.coins_add_cp(char.get("coins"), gain_cp)
    room.persist_character(char)
    text = f"{char['name']} 卖出 {item.get('name')}×{qty}（半价），获得 {_money_text(gain_cp)}，余额 {characters_mod.coins_text(char['coins'])}"
    return _result(text, notes=[text], changed=True)


# ---------------------------------------------------------------------------
# 战役与时钟动作
# ---------------------------------------------------------------------------


def _update_quest(room, args):
    if not room.campaign:
        raise ValueError("尚未设置战役结构")
    summary = campaign_mod.update_quest(
        room.campaign, args.get("quest", ""), args.get("status", ""), args.get("note", "")
    )
    room.persist_room_meta()
    return _result(summary, notes=[summary], changed=True)


def _add_world_note(room, args):
    if not room.campaign:
        raise ValueError("尚未设置战役结构")
    text = campaign_mod.add_world_note(
        room.campaign, args.get("kind", ""), args.get("name", ""), args.get("note", "")
    )
    room.persist_room_meta()
    return _result(text)


def _advance_pressure(room, args):
    if not room.campaign:
        raise ValueError("尚未设置战役结构")
    summary = campaign_mod.advance_pressure(room.campaign, args.get("amount", 1), args.get("reason", ""))
    room.persist_room_meta()
    return _result(summary, notes=[summary], changed=True)


def _advance_time(room, args):
    try:
        minutes = float(args.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes <= 0 or minutes > 60 * 24 * 14:
        return _result("时间推进数值不合理（应为 1 分钟至 14 天之间）")
    expired = state_mod.advance_clock(room, minutes)
    text = (
        f"时间推进 {int(minutes)} 分钟，现在是 {state_mod.format_clock(room.clock_minutes)}"
        f"（{args.get('reason', '')}）"
    )
    if expired:
        text += "；" + "；".join(expired)
    return _result(text, notes=expired, changed=True)


def _short_rest(room, args):
    notes = state_mod.short_rest(room)
    text = "短休完成（1 小时）" + ("：" + "；".join(notes) if notes else "")
    return _result(text, notes=notes, changed=True)


def _long_rest(room, args):
    notes = state_mod.long_rest(room)
    text = "长休完成（8 小时）" + ("：" + "；".join(notes) if notes else "")
    return _result(text, notes=notes, changed=True)


# ---------------------------------------------------------------------------
# 法术与成长动作
# ---------------------------------------------------------------------------


def _cast_spell(room, args):
    caster = state_mod.find_character(room, args.get("caster", ""))
    if not caster:
        raise ValueError(f"找不到施法者：{args.get('caster', '')}")
    found = spells_mod.find_spell(args.get("spell", ""))
    if not found:
        raise ValueError(f"找不到法术：{args.get('spell', '')}")
    as_ritual = bool(args.get("ritual"))
    summary, _meta = spells_mod.cast_spell(
        caster, found[0], args.get("slot_level"), room.clock_minutes, as_ritual=as_ritual
    )
    room.persist_character(caster)
    if as_ritual:
        # 仪式施法约 10 分钟：引擎自动推进时钟（效果计时随之结算）
        state_mod.advance_clock(room, 10)
        summary += "；仪式耗时 10 分钟（时钟已推进）"
    return _result(summary, notes=[summary], changed=True)


def _learn_spell(room, args):
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    found = spells_mod.find_spell(args.get("spell", ""))
    if not found:
        raise ValueError(f"找不到法术：{args.get('spell', '')}")
    summary = spells_mod.learn_spell(
        char, found[0], found[1],
        bucket=args.get("bucket") or None,
        free=bool(args.get("free", True)),
    )
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _forget_spell(room, args):
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    summary = spells_mod.forget_spell(char, args.get("spell", ""), bucket=args.get("bucket") or None)
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _swap_spell(room, args):
    """已知型职业替换法术（每级 1 个名额）：old → new（来源/环阶/去重校验）。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    found = spells_mod.find_spell(args.get("spell", ""))
    if not found:
        raise ValueError(f"找不到新法术：{args.get('spell', '')}")
    summary = spells_mod.swap_spell(char, args.get("old", ""), found[0], found[1])
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _add_effect(room, args):
    char = state_mod.find_character(room, args.get("target", ""))
    if not char:
        raise ValueError(f"找不到目标：{args.get('target', '')}")
    rounds = args.get("rounds", 1)
    try:
        minutes = max(0.0, float(rounds)) * 0.1
    except (TypeError, ValueError):
        minutes = 0.1
    summary = spells_mod.add_effect(
        char,
        args.get("name", ""),
        minutes,
        clock_minutes=room.clock_minutes,
        concentration=bool(args.get("concentration")),
        note=args.get("note", ""),
    )
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _remove_effect(room, args):
    char = state_mod.find_character(room, args.get("target", ""))
    if not char:
        raise ValueError(f"找不到目标：{args.get('target', '')}")
    summary = spells_mod.remove_effect(char, args.get("name", ""))
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _level_up(room, args):
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    summary = spells_mod.level_up(char)
    if char.get("companion"):
        # 同伴没有玩家：待分配的属性提升按主属性自动结算；子职业/恩赐自动取第一项
        settled = characters_mod.settle_companion_asi(char)
        progressed = characters_mod.settle_companion_progression(char)
        extra = "；".join(x for x in (settled, progressed) if x)
        if extra:
            summary += "；" + extra
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _level_up_party(room, args):
    """全队里程碑升级：为每位上桌角色（含同伴）各升 1 级；已达上限者跳过并说明。"""
    seated = [
        char for char in room.characters.values()
        if char.get("owner_sid") or char.get("companion")
    ]
    if not seated:
        raise ValueError("队伍里还没有上桌角色")
    parts = []
    for char in seated:
        try:
            summary = spells_mod.level_up(char)
        except ValueError as exc:
            parts.append(f"{char.get('name', '')} 未能升级：{exc}")
            continue
        if char.get("companion"):
            settled = characters_mod.settle_companion_asi(char)
            if settled:
                summary += "；" + settled
        room.persist_character(char)
        parts.append(summary)
    # 明确提示待分配项（子职业/提升/恩赐）——引导 GM/玩家打开对应角色卡
    pending = []
    for char in seated:
        items = []
        if char.get("pending_subclass"):
            items.append("子职业")
        count = int(char.get("pending_asi") or 0)
        if count:
            items.append(f"提升 ×{count}")
        boons = len(char.get("pending_boons") or [])
        if boons:
            items.append(f"恩赐 ×{boons}")
        if items:
            pending.append(f"{char.get('name', '')}（{'、'.join(items)}）")
    if pending:
        parts.append("待分配：" + "；".join(pending) + " —— 打开对应角色卡即可分配（同伴由系统自动结算）")
    return _result("；".join(parts), notes=parts, changed=True)


def _apply_asi(room, args):
    """
    分配一次属性提升（BFRD 的 Improvement，4/8/12/16/19 级获得）：
      +2 一项 / +1 两项 / 一项 +1 并选一项职业提升天赋（args.talent）。
    接受 asi={"str":2} 形式，也接受 AI 工具的 plus_two / plus_one / talent 参数形式。
    """
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    if int(char.get("pending_asi", 0)) <= 0:
        raise ValueError("没有待分配的属性提升")
    asi = args.get("asi")
    if not isinstance(asi, dict) or not asi:
        asi = {}
        if args.get("plus_two"):
            asi[str(args["plus_two"])] = 2
        for key in args.get("plus_one") or []:
            asi[str(key)] = asi.get(str(key), 0) + 1
    summary = characters_mod.apply_asi(char, asi, talent_id=str(args.get("talent") or "") or None)
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _choose_subclass(room, args):
    """选择子职业（3 级解锁；本人 / GM / AI 均可，引擎校验归属与重复）。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    summary = characters_mod.choose_subclass(char, str(args.get("subclass", "")))
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _choose_boon(room, args):
    """选择恩赐（10 级英雄恩赐 / 20 级史诗恩赐）；level 省略时取最小的待选等级。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    pending = [int(x) for x in (char.get("pending_boons") or [])]
    if not pending:
        raise ValueError("没有待选的恩赐")
    level = int(args.get("level") or min(pending))
    summary = characters_mod.choose_boon(char, level, str(args.get("boon", "")))
    room.persist_character(char)
    return _result(summary, notes=[summary], changed=True)


def _grant_luck(room, args):
    """
    授予幸运（BFRD 幸运系统）：失败积累 / GM 嘉奖。
    满 5 点再获得时掷 d4 重置（幸运之触：1d4+1）；失败获得时幸运之触翻倍。
    """
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    try:
        amount = int(args.get("amount") or 1)
    except (TypeError, ValueError):
        raise ValueError("授予数量不合法")
    if amount <= 0:
        raise ValueError("授予数量必须为正")
    d4 = engine.roll_expr("1d4")["total"] if int(char.get("luck") or 0) >= 5 else None
    _, note = characters_mod.luck_add(char, amount, d4_roll=d4, from_failure=bool(args.get("from_failure")))
    room.persist_character(char)
    reason = str(args.get("reason") or "").strip()
    text = f"{char.get('name', '')}：{note}" + (f"（{reason}）" if reason else "")
    return _result(text, notes=[text], changed=True)


def _spend_luck(room, args):
    """消耗幸运（1 对 1 加值 / 3 点重掷由叙事决定，引擎校验余额并扣减）。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    try:
        amount = int(args.get("amount") or 1)
    except (TypeError, ValueError):
        raise ValueError("消耗数量不合法")
    remaining = characters_mod.luck_spend(char, amount)
    room.persist_character(char)
    reason = str(args.get("reason") or "").strip()
    text = f"{char.get('name', '')} 消耗 {amount} 点幸运（剩余 {remaining}/5）" + (f"（{reason}）" if reason else "")
    return _result(text, notes=[text], changed=True)


def _skill_check(room, args):
    """通用技能/属性检定：引擎自动取调整值/熟练并处理力竭劣势；支持优势/劣势与 DC。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    check = characters_mod.skill_check(
        char,
        skill_id=args.get("skill") or None,
        ability=args.get("ability") or None,
        dc=args.get("dc"),
        mode=str(args.get("mode") or "normal"),
    )
    breakdown = check.get("breakdown", "")
    text = f"{char.get('name', '')} {check.get('label', '')}检定：{breakdown}"
    if check.get("dc") is not None:
        text += f" → 对抗 DC {check['dc']}：{'成功' if check.get('success') else '失败'}"
    if int(check.get("exhaustion") or 0) >= 1 and str(args.get("mode") or "normal") == "normal":
        text += f"（力竭 {check['exhaustion']} 级：自动劣势）"
    record = {
        "who": "AI GM",
        "reason": f"{check.get('label', '')}检定",
        "expr": breakdown,
        "total": check.get("total"),
        "breakdown": breakdown,
        "dc": check.get("dc"),
        "success": check.get("success"),
        "mode": check.get("mode"),
    }
    return _result(text, notes=[text], roll_record=record)


def _stabilize(room, args):
    """稳定化（BFRD）：救助者以 DC 10 感知（医药）检定稳定一个濒死/倒地的同伴。"""
    target = state_mod.find_character(room, args.get("target", "") or args.get("character", ""))
    if not target:
        raise ValueError(f"找不到目标：{args.get('target', '')}")
    if not (target.get("dying") or target.get("stable")) or int(target["hp"]["cur"]) > 0:
        raise ValueError("目标不需要稳定（仅 0 HP 的濒死角色需要）")
    healer = state_mod.find_character(room, args.get("by", "")) or target
    check = characters_mod.skill_check(healer, "medicine", dc=10)
    text = f"{healer.get('name', '')} 尝试稳定 {target.get('name', '')}：{check.get('breakdown', '')} → 对抗 DC 10：{'成功' if check.get('success') else '失败'}"
    if check.get("success"):
        d4 = engine.roll_expr("1d4")["total"]
        text += "；" + characters_mod.set_stable(target, stable_until=room.clock_minutes + d4 * 60)
        text += f"（约 {d4} 小时后自动回复 1 HP）"
    else:
        text += "；稳定失败（可再次尝试）"
    room.persist_character(target)
    record = {"who": "AI GM", "reason": "稳定化（医药）", "expr": check.get("breakdown", ""), "total": check.get("total"), "breakdown": check.get("breakdown", ""), "dc": 10, "success": check.get("success")}
    return _result(text, notes=[text], roll_record=record, changed=bool(check.get("success")))


def _revive(room, args):
    """复活（BFRD）：施法者消耗对应法术位，让已死亡角色重返人世（回复 1 HP）。"""
    caster = state_mod.find_character(room, args.get("caster", "") or args.get("by", ""))
    target = state_mod.find_character(room, args.get("character", "") or args.get("target", ""))
    if not caster or not target:
        raise ValueError("需要指定施法者（caster）与目标（character）")
    if not target.get("dead"):
        raise ValueError(f"{target.get('name', '')} 未死亡，无需复活法术")
    known = {s.get("id") for s in (caster.get("spells") or [])}
    pick = str(args.get("spell") or "").strip()
    if pick:
        if pick not in characters_mod.REVIVAL_SPELLS:
            raise ValueError("该法术不能复活死者")
        if pick not in known:
            raise ValueError(f"{caster.get('name', '')} 未习得该法术")
    else:
        pick = next((sid for sid in characters_mod.REVIVAL_SPELLS if sid in known), "")
        if not pick:
            raise ValueError("施法者没有已知的复活法术（复苏术/死者复活/复活术/真实复活术）")
    entry = srd.spells()[pick]
    level = int(entry.get("level") or 0)
    slots = caster.setdefault("spell_slots", {})
    slot = slots.get(str(level))
    if not slot or slot.get("used", 0) >= slot.get("max", 0):
        raise ValueError(f"没有可用的 {level} 环法术位施展「{entry['name']}」")
    slot["used"] = slot.get("used", 0) + 1
    target["dead"] = False
    target["hp"]["cur"] = 1
    characters_mod.stop_dying(target)
    effects = target.setdefault("effects", [])
    if not any(e.get("name") == "倒地" for e in effects):
        spells_mod.add_effect(target, "倒地", 0, room.clock_minutes, note="复活后仍倒地")
    room.persist_character(caster)
    room.persist_character(target)
    text = (
        f"{caster.get('name', '')} 施展「{entry['name']}」（消耗 {level} 环法术位）复活 {target.get('name', '')}："
        f"回复 1 点生命值、解除昏迷（仍倒地）。具体副作用/恢复量按法术描述由 GM 叙事裁定。"
    )
    return _result(text, notes=[text], changed=True)


def _adjust_exhaustion(room, args):
    """力竭增减（BFRD 六档）：1 检定劣势 / 2 速度减半 / 3 攻击豁免劣势 / 4 HP 上限减半 / 5 速度 0 / 6 死亡。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    try:
        delta = int(args.get("delta", 0))
    except (TypeError, ValueError):
        raise ValueError("力竭变化数值不合法")
    current = int(char.get("exhaustion") or 0)
    text = characters_mod.apply_exhaustion(char, current + delta)
    reason = str(args.get("reason") or "").strip()
    if reason:
        text += f"（{reason}）"
    room.persist_character(char)
    return _result(text, notes=[text], changed=True)


# Downtime（冒险之间）活动表（BFRD 07 章原文数值；金额单位铜）
_DOWNTIME_TIERS = {"lower": 1000, "middle": 5000, "upper": 25000}      # 狂欢每周每人花费（gp×100）
_DOWNTIME_TIER_NAMES = {"lower": "下层阶级", "middle": "中层阶级", "upper": "上层阶级"}


def _downtime_pay(room, char, cost_cp, label):
    """Downtime 支出：钱不够抛错；成功则扣除（coins_change_cp 返回新字典，须回写）。"""
    if cost_cp <= 0:
        return
    try:
        char["coins"] = characters_mod.coins_change_cp(char["coins"], -int(cost_cp))
    except ValueError:
        raise ValueError(f"金币不足：{label}需要 {cost_cp // 100} 金 {cost_cp % 100} 铜（当前总值 {characters_mod.coin_total_cp(char['coins']) // 100} 金）")


def _downtime_carousing(room, char, args):
    weeks = max(1, min(4, int(args.get("weeks") or 1)))
    tier = str(args.get("tier") or "lower")
    if tier not in _DOWNTIME_TIERS:
        raise ValueError("狂欢阶级需要 lower / middle / upper")
    cost = _DOWNTIME_TIERS[tier] * weeks
    _downtime_pay(room, char, cost, f"{_DOWNTIME_TIER_NAMES[tier]}狂欢 {weeks} 周")
    state_mod.advance_clock(room, weeks * 7 * 1440)
    contacts = int(char.get("contacts") or 0)
    favors = int(char.get("favors") or 0)
    rolls = []
    for _ in range(weeks):
        check = characters_mod.skill_check(char, "persuasion")
        total = int(check.get("total") or 0)
        if total <= 5:
            outcome = "失去一个人脉或一份人情（引擎扣减）"
            if favors > 0:
                favors -= 1
            elif contacts > 0:
                contacts -= 1
        elif total <= 10:
            outcome = "无收获"
        elif total <= 15:
            outcome = "结识 1 个人脉"
            contacts += 1
        elif total <= 20:
            outcome = "结识 2 个人脉"
            contacts += 2
        else:
            outcome = "结识 3 个人脉"
            contacts += 3
        rolls.append(f"{check.get('breakdown', '')} → {outcome}")
    char["contacts"], char["favors"] = contacts, favors
    text = (
        f"{char.get('name', '')} 在{_DOWNTIME_TIER_NAMES[tier]}狂欢 {weeks} 周（花费 {cost // 100} 金）；"
        + "；".join(rolls)
        + f"（现有人脉 {contacts}、人情 {favors}）"
    )
    return text


def _downtime_research(room, char, args):
    weeks = max(1, min(4, int(args.get("weeks") or 1)))
    skill = str(args.get("skill") or "history")
    if skill not in ("arcana", "history", "nature", "religion"):
        raise ValueError("研究检定技能需为 奥秘/历史/自然/宗教")
    topic = str(args.get("topic") or "研究主题")[:40]
    cost = 5000 * weeks
    _downtime_pay(room, char, cost, f"研究（{topic}）{weeks} 周")
    state_mod.advance_clock(room, weeks * 7 * 1440)
    clues = int(char.get("clues") or 0)
    rolls = []
    for _ in range(weeks):
        check = characters_mod.skill_check(char, skill)
        total = int(check.get("total") or 0)
        if total <= 5:
            outcome = "得到一条错误线索（引擎不计数）"
        elif total <= 10:
            outcome = "无收获"
        elif total <= 15:
            clues += 1
            outcome = "得到 1 条线索"
        elif total <= 20:
            clues += 2
            outcome = "得到 2 条线索"
        else:
            clues += 3
            outcome = "得到 3 条线索"
        rolls.append(f"{check.get('breakdown', '')} → {outcome}")
    char["clues"] = clues
    return (
        f"{char.get('name', '')} 研究「{topic}」{weeks} 周（花费 {cost // 100} 金）；"
        + "；".join(rolls)
        + f"（现有线索 {clues}；线索可换取 GM 保证的真实事实，或让相关智力检定自动成功）"
    )


def _downtime_crafting(room, char, args):
    days = max(1, min(30, int(args.get("days") or 1)))
    current = char.get("crafting")
    item_ref = str(args.get("item") or "").strip()
    if current and item_ref and item_ref not in (current.get("item") or "", current.get("name") or ""):
        raise ValueError(f"已有进行中的制作（{current.get('name')}），请先完成")
    if not current:
        if not item_ref:
            raise ValueError("开始制作需要指定物品（item，库内 id/中文名）或自定义名称+价格")
        entry = None
        idx = None
        for i, candidate in srd.items().items():
            if i == item_ref or candidate.get("name") == item_ref:
                idx, entry = i, candidate
                break
        if entry and entry.get("cost_cp"):
            price = int(entry["cost_cp"])
            name = entry.get("name", item_ref)
        else:
            price = int(args.get("price_cp") or 0)
            if price <= 0:
                raise ValueError("该物品没有定价：请用 price_cp 提供成品总价（铜）")
            name = item_ref
        materials = price // 2
        _downtime_pay(room, char, materials, f"制作「{name}」的材料（半价）")
        current = {"item": idx, "name": name, "price_cp": price, "progress_cp": 0}
        char["crafting"] = current
    progress = int(current.get("progress_cp") or 0) + days * 1000
    price = int(current.get("price_cp") or 0)
    current["progress_cp"] = min(progress, price)
    state_mod.advance_clock(room, days * 1440)
    if current["progress_cp"] >= price:
        char["crafting"] = None
        item = {"id": current.get("item"), "name": current.get("name"), "qty": 1}
        char.setdefault("items", []).append(item)
        return f"{char.get('name', '')} 制作完成：「{current.get('name')}」已入背包（共 {price // 100} 金价值，耗时累计 {price // 1000} 天）"
    left = price - current["progress_cp"]
    return (
        f"{char.get('name', '')} 制作「{current.get('name')}」：+{days} 天进度（每天 10 金），"
        f"累计 {current['progress_cp'] // 100} / {price // 100} 金，还差约 {-(-left // 1000)} 天"
    )


def _downtime_training(room, char, args):
    weeks = max(1, min(8, int(args.get("weeks") or 1)))
    goal = str(args.get("goal") or "一项新熟练或天赋")[:40]
    required = int(args.get("required_weeks") or 50)
    cost = 100 * weeks
    _downtime_pay(room, char, cost, f"训练（{goal}）{weeks} 周")
    state_mod.advance_clock(room, weeks * 7 * 1440)
    state = char.get("training") or {"goal": goal, "progress_weeks": 0, "required_weeks": required}
    if state.get("goal") != goal:
        state = {"goal": goal, "progress_weeks": 0, "required_weeks": required}
    rolls = []
    for _ in range(weeks):
        prof = int(char.get("prof_bonus") or 2)
        roll = engine.roll_expr(f"1d20{prof:+d}")
        total = roll["total"]
        if total <= 5:
            outcome = "损失一周进展（或失去教导者）"
            state["progress_weeks"] = max(0, int(state["progress_weeks"]) - 1)
        elif total <= 10:
            state["progress_weeks"] = int(state["progress_weeks"]) + 1
            outcome = "进展 1 周"
        elif total <= 15:
            state["progress_weeks"] = int(state["progress_weeks"]) + 2
            outcome = "进展 2 周"
        elif total <= 20:
            gain = max(3, int(char.get("prof_bonus") or 2))
            state["progress_weeks"] = int(state["progress_weeks"]) + gain
            outcome = f"进展 {gain} 周"
        else:
            remain = max(1, int(state["required_weeks"]) - int(state["progress_weeks"]))
            state["progress_weeks"] = int(state["progress_weeks"]) + remain // 2
            outcome = "剩余周数减半"
        rolls.append(f"{roll['breakdown']} → {outcome}")
    char["training"] = state
    done = int(state["progress_weeks"]) >= int(state["required_weeks"])
    text = (
        f"{char.get('name', '')} 训练「{goal}」{weeks} 周（花费 {cost // 100} 金）；"
        + "；".join(rolls)
        + f"（进度 {state['progress_weeks']}/{state['required_weeks']} 周"
    )
    if done:
        char["training"] = None
        text += "）训练完成！请 GM 把这项新熟练/天赋写入角色卡（熟练可直接补，天赋请与 GM 确认后手动添加）"
    else:
        text += "）"
    return text


def _downtime_working(room, char, args):
    weeks = max(1, min(8, int(args.get("weeks") or 1)))
    skill = str(args.get("skill") or "").strip() or None
    ability = None if skill else str(args.get("ability") or "str")
    state_mod.advance_clock(room, weeks * 7 * 1440)
    rolls = []
    income_cp = 0
    for _ in range(weeks):
        check = characters_mod.skill_check(char, skill_id=skill, ability=ability)
        total = int(check.get("total") or 0)
        if total <= 5:
            income_cp += 100
            outcome = "10 银"
        elif total <= 10:
            income_cp += 500
            outcome = "5 金"
        elif total <= 15:
            income_cp += 1000
            outcome = "10 金"
        elif total <= 20:
            income_cp += 2000
            outcome = "20 金"
        else:
            extra = 500 * int(char.get("prof_bonus") or 2)
            income_cp += 2000 + extra
            outcome = f"20 金 + {extra // 100} 金（PB×5）"
        rolls.append(f"{check.get('breakdown', '')} → {outcome}")
    char["coins"] = characters_mod.coins_change_cp(char["coins"], income_cp)
    return (
        f"{char.get('name', '')} 工作 {weeks} 周；" + "；".join(rolls)
        + f"（共收入 {income_cp // 100} 金 {income_cp % 100} 银/铜，已入钱袋）"
    )


def _downtime(room, args):
    """
    Downtime（冒险之间）统一入口（BFRD 07）：引擎自动收钱、推进时钟、掷骰查表、记账。
    activity：carousing 狂欢 / researching 研究 / crafting 制作 / training 训练 / working 工作。
    """
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    if char.get("dead") or char.get("dying") or char.get("stable"):
        raise ValueError("该角色当前无法进行休整期活动（昏迷/濒死/死亡）")
    activity = str(args.get("activity") or "").strip().lower()
    handlers = {
        "carousing": _downtime_carousing,
        "researching": _downtime_research,
        "crafting": _downtime_crafting,
        "training": _downtime_training,
        "working": _downtime_working,
    }
    if activity not in handlers:
        raise ValueError("未知休整活动（carousing/researching/crafting/training/working）")
    text = handlers[activity](room, char, args)
    room.persist_character(char)
    return _result(text, notes=[text], changed=True)


def _wear_armor(room, args):
    """更换穿着（信任制）：只能穿背包里的护甲条目（盾牌独立开关）；重算 AC。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    armor_raw = args.get("armor")
    if armor_raw in (None, "", "none"):
        key = "none"
    else:
        key = characters_mod.resolve_armor_key(armor_raw)
        entry = characters_mod.armor_items().get(key) or {}
        owned = {it.get("id") for it in (char.get("items") or []) if it.get("id")}
        implicit = characters_mod.resolve_armor_key(char.get("armor"))   # 旧字段隐式持有（建卡显式指定）
        if not key or key == "none" or entry.get("is_shield") or (key not in owned and key != implicit):
            raise ValueError("只能穿着背包里的护甲（且不能把盾牌当护甲穿）")
    shield = bool(args.get("shield", char.get("worn_shield", char.get("shield"))))
    char["worn_armor"] = key
    char["worn_shield"] = shield
    characters_mod.recompute_derived(char)
    room.persist_character(char)
    label = "无甲" if key == "none" else (characters_mod.armor_items().get(key) or {}).get("label", key)
    text = f"{char.get('name', '')} 换上「{label}」{'+ 盾牌' if shield else ''}（AC {char['ac']}）"
    return _result(text, notes=[text], changed=True)


def _spend_hit_dice(room, args):
    """
    短休消耗生命骰回复生命（BFRD）：每颗掷 dX + 体质调整值（至少 1），
    上限为等级数、长休恢复一半。信任制：由本人/GM/AI 在短休场景调用。
    """
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    if char.get("dead"):
        raise ValueError("已死亡角色无法消耗生命骰")
    if char.get("dying") or char.get("stable"):
        raise ValueError("昏迷/濒死角色无法自行消耗生命骰（需要治疗或稳定苏醒）")
    level = int(char.get("level") or 1)
    used = int(char.get("hit_dice_used") or 0)
    try:
        count = int(args.get("count") or 1)
    except (TypeError, ValueError):
        raise ValueError("生命骰数量不合法")
    if count < 1:
        raise ValueError("至少消耗 1 颗生命骰")
    if used + count > level:
        raise ValueError(f"生命骰不足（剩余 {level - used} 颗）")
    die = int(char.get("hit_die") or 8)
    con_mod = int((char.get("mods") or {}).get("con", 0))
    rolls, gains = [], 0
    for _ in range(count):
        roll = engine.roll_expr(f"1d{die}")
        value = max(1, int(roll["total"]) + con_mod)
        gains += value
        rolls.append(f"d{die}({roll['total']}){con_mod:+d}={value}")
    before = int(char["hp"]["cur"])
    char["hp"]["cur"] = min(int(char["hp"]["max"]), before + gains)
    char["hit_dice_used"] = used + count
    room.persist_character(char)
    for combatant in room.combat.get("combatants", []):
        if combatant.get("id") == char["id"]:
            combatant["hp"] = char["hp"]["cur"]
            break
    text = (
        f"{char.get('name', '')} 消耗 {count} 颗生命骰（{', '.join(rolls)}）："
        f"回复 {char['hp']['cur'] - before} 点，HP {char['hp']['cur']}/{char['hp']['max']}；"
        f"剩余生命骰 {level - used - count}/{level}"
    )
    return _result(text, notes=[text], changed=True)


# 载具（轻量版）：HP/AC 追踪 + 伤害阈值 + 修理（BFRD 06 章）
_VEHICLE_HP_RE = re.compile(r"生命值\D*(\d+)")
_VEHICLE_AC_RE = re.compile(r"护甲等级\D*(\d+)")
_VEHICLE_THRESHOLD_RE = re.compile(r"伤害阈值\D*(\d+)")


def _find_vehicle(room, char, query):
    """在角色背包里找载具条目（库内 category=载具）；返回 (item_entry, db_entry)。"""
    key = str(query or "").strip()
    for item in char.get("items") or []:
        if not item.get("id"):
            continue
        db = srd.items().get(item["id"]) or {}
        if db.get("category_zh") != "载具":
            continue
        if key and key not in (item.get("id"), item.get("name", "")) and key not in item.get("name", ""):
            continue
        item.setdefault("hp_max", int((_VEHICLE_HP_RE.search(db.get("desc_zh") or "") or [None, 50])[1]))
        item.setdefault("hp", item["hp_max"])
        item.setdefault("ac", int((_VEHICLE_AC_RE.search(db.get("desc_zh") or "") or [None, 15])[1]))
        item.setdefault("threshold", int((_VEHICLE_THRESHOLD_RE.search(db.get("desc_zh") or "") or [None, 0])[1]))
        return item, db
    return None, None


def _damage_vehicle(room, args):
    """载具受损/修复：单次伤害低于伤害阈值则不受损；0 HP 载具被摧毁（无法再修理）。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    item, db = _find_vehicle(room, char, args.get("vehicle", ""))
    if not item:
        raise ValueError("背包里找不到该载具（先用 add_item 把载具加入某人背包）")
    try:
        delta = int(args.get("delta") or 0)
    except (TypeError, ValueError):
        raise ValueError("载具 HP 变化数值不合法")
    name = item.get("name") or db.get("name", "")
    if delta < 0:
        if bool(args.get("single_hit")) and abs(delta) < int(item.get("threshold") or 0):
            text = f"{name} 单次受击 {abs(delta)} 点低于伤害阈值 {item['threshold']}：载具不受伤害"
            return _result(text, notes=[text], changed=False)
        item["hp"] = max(0, int(item.get("hp") or 0) + delta)
        text = f"{name} 受损 {-delta}：HP {item['hp']}/{item['hp_max']}"
        if item["hp"] == 0:
            text += "；载具被摧毁（无法修理）"
    else:
        item["hp"] = min(int(item.get("hp_max") or 0), int(item.get("hp") or 0) + delta)
        text = f"{name} 恢复 {delta}：HP {item['hp']}/{item['hp_max']}"
    room.persist_character(char)
    return _result(text, notes=[text], changed=True)


def _repair_vehicle(room, args):
    """载具修理（BFRD）：每小时消耗基础价 10% 的部件，恢复等于 AC 值的 HP；0 HP 无法修理。"""
    char = state_mod.find_character(room, args.get("character", ""))
    if not char:
        raise ValueError(f"找不到角色：{args.get('character', '')}")
    item, db = _find_vehicle(room, char, args.get("vehicle", ""))
    if not item:
        raise ValueError("背包里找不到该载具")
    if int(item.get("hp") or 0) <= 0:
        raise ValueError("载具已被摧毁，BFRD 规则下无法修理")
    try:
        hours = max(1, min(8, int(args.get("hours") or 1)))
    except (TypeError, ValueError):
        raise ValueError("修理小时数不合法")
    base_cost = int(db.get("cost_cp") or 0)
    cost = (base_cost // 10) * hours
    _downtime_pay(room, char, cost, f"修理{db.get('name', '')} {hours} 小时（部件）")
    healed = min(int(item.get("ac") or 0) * hours, int(item.get("hp_max") or 0) - int(item.get("hp") or 0))
    item["hp"] = int(item.get("hp") or 0) + healed
    state_mod.advance_clock(room, hours * 60)
    room.persist_character(char)
    text = (
        f"{char.get('name', '')} 修理{db.get('name', '')} {hours} 小时（耗部件 {cost // 100} 金）："
        f"恢复 {healed} HP（{item['hp']}/{item['hp_max']}，每小时按 AC {item['ac']} 恢复）"
    )
    return _result(text, notes=[text], changed=True)


def _recruit_companion(room, args):
    companion = state_mod.create_companion(
        room,
        args.get("name", ""),
        args.get("klass", ""),
        args.get("race") or "human",
        args.get("level") or 1,
        args.get("note", ""),
    )
    summary = (
        f"同伴「{companion['name']}」加入队伍"
        f"（{companion['race']}{companion['klass']} {companion['level']}级，HP {companion['hp']['max']}）"
    )
    return _result(summary, notes=[summary], changed=True)


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

_HANDLERS = {
    "get_party": _get_party,
    "get_campaign": _get_campaign,
    "query_monsters": _query_monsters,
    "roll_dice": _roll_dice,
    "spawn_monsters": _spawn_monsters,
    "move_combatant_toward": _move_combatant_toward,
    "advance_turn": _advance_turn,
    "adjust_hp": _adjust_hp,
    "end_combat": _end_combat,
    "update_quest": _update_quest,
    "add_world_note": _add_world_note,
    "advance_pressure": _advance_pressure,
    "advance_time": _advance_time,
    "short_rest": _short_rest,
    "long_rest": _long_rest,
    "cast_spell": _cast_spell,
    "learn_spell": _learn_spell,
    "forget_spell": _forget_spell,
    "swap_spell": _swap_spell,
    "add_effect": _add_effect,
    "remove_effect": _remove_effect,
    "level_up": _level_up,
    "level_up_party": _level_up_party,
    "apply_asi": _apply_asi,
    "choose_subclass": _choose_subclass,
    "choose_boon": _choose_boon,
    "grant_luck": _grant_luck,
    "spend_luck": _spend_luck,
    "skill_check": _skill_check,
    "stabilize": _stabilize,
    "revive": _revive,
    "adjust_exhaustion": _adjust_exhaustion,
    "downtime": _downtime,
    "wear_armor": _wear_armor,
    "spend_hit_dice": _spend_hit_dice,
    "damage_vehicle": _damage_vehicle,
    "repair_vehicle": _repair_vehicle,
    "get_inventory": _get_inventory,
    "add_item": _add_item,
    "remove_item": _remove_item,
    "set_item_note": _set_item_note,
    "adjust_gold": _adjust_gold,
    "set_coins": _set_coins,
    "exchange_currency": _exchange_currency,
    "buy_item": _buy_item,
    "sell_item": _sell_item,
    "reward_party": _reward_party,
    "recruit_companion": _recruit_companion,
}

# 供 UI/校验使用：人类 GM 控制台可调用的动作名集合
KNOWN_ACTIONS = tuple(_HANDLERS)


def perform(room, name, args):
    """
    统一动作入口：按名字分发到实现。未知动作抛 ValueError。
    调用方（AI 对话循环 / WS 通用 action 消息）负责：广播 notes、按 changed 刷快照、
    处理 roll_record 与 attach_board。
    """
    handler = _HANDLERS.get(str(name or ""))
    if handler is None:
        raise ValueError(f"未知动作：{name}")
    return handler(room, args or {})
