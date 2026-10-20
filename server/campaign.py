"""
战役结构：模组骨架的数据模型、校验、运行时进度与渲染。

定位：结构是 AI GM 的"硬约束清单"（镇子/任务链/地点/关键转折/终局），
也是玩家"任务板"的数据来源。完整结构（含 secret 剧透字段）只进 AI 上下文
与存档；公开视图（public_view）剥掉剧透后再发给浏览器。

数据模型（存于 RoomState.campaign，随存档保存）：
{
  "format": "weavlum-campaign-structure", "version": 1,
  "title": 战役名, "theme": 主题一行,
  "pressure_type": "threat_clock" | "roaming_threat" | "boss_readiness",
  "pressure": {"label": 压力名, "max": 任务数+1, "level": 0},
  "hub": {"name": 枢纽镇, "desc": 一句话, "giver": 发布人},
  "quests": [ {id, tier(1-3), title, location, giver, objective, hook, secret,
               encounters, reward, completion, fail, status, notes[]} ],
  "finale": {"location", "objective", "condition", "endings": [3 个结局倾向]},
  "world": {"locations": [], "npcs": [], "clues": []}    # AI 运行时登记（add_world_note）
}

任务状态机：undiscovered → in_progress → completed / failed / expired
解锁规则：每阶 3 个任务，上一阶完成 ≥2 个解锁下一阶；
终局在第三阶完成 ≥2 个或压力满格时开启。
进度条机制（threat_clock）：任务完成自动推进 1 格；上限默认为任务数 + 1（留一步余量，
避免"任务全清必然满格"锁死结局路线）；AI 可用 advance_pressure 额外推进。
"""

import time

VALID_PRESSURE_TYPES = ("threat_clock", "roaming_threat", "boss_readiness")
VALID_STATUS = ("undiscovered", "in_progress", "completed", "failed", "expired")
NOTE_KINDS = ("locations", "npcs", "clues")
NOTE_LABELS = {"locations": "地点", "npcs": "NPC", "clues": "线索"}


def normalize_structure(data):
    """补齐默认值（status/notes/world/pressure.level 等），返回可入库的结构。"""
    data = dict(data)
    data.setdefault("format", "weavlum-campaign-structure")
    data.setdefault("version", 1)
    data.setdefault("title", "未命名战役")
    data.setdefault("theme", "")
    data.setdefault("pressure_type", "threat_clock")
    pressure = data.get("pressure")
    if not isinstance(pressure, dict):
        pressure = {}
    pressure.setdefault("label", "威胁进度")
    raw_quests = data.get("quests") or []
    quest_count = len(raw_quests) if isinstance(raw_quests, list) else 0
    pressure.setdefault("max", quest_count + 1 if quest_count else 6)
    pressure.setdefault("level", 0)
    data["pressure"] = pressure
    hub = data.get("hub")
    if not isinstance(hub, dict):
        hub = {}
    data["hub"] = hub
    quests = []
    for quest in data.get("quests") or []:
        quest = dict(quest)
        quest.setdefault("status", "undiscovered")
        quest.setdefault("notes", [])
        quests.append(quest)
    data["quests"] = quests
    finale = data.get("finale")
    data["finale"] = finale if isinstance(finale, dict) else {}
    data.setdefault("finale_cleared", False)
    world = data.get("world")
    if not isinstance(world, dict):
        world = {}
    for kind in NOTE_KINDS:
        if not isinstance(world.get(kind), list):
            world[kind] = []
    data["world"] = world
    return data


def reset_progress(campaign):
    """
    重置战役的运行时进度（结构与文案保留，供同一战役从头重跑）：
    任务状态回到"未发现"、进展注记清空、世界登记（地点/NPC/线索）清空、
    压力归零、终局标记清除。返回是否真的重置了（未设置结构时 False）。
    """
    if not isinstance(campaign, dict):
        return False
    for quest in campaign.get("quests") or []:
        quest["status"] = "undiscovered"
        quest["notes"] = []
    pressure = campaign.get("pressure")
    if isinstance(pressure, dict):
        pressure["level"] = 0
    world = campaign.get("world")
    if not isinstance(world, dict):
        world = {}
    for kind in NOTE_KINDS:
        world[kind] = []
    campaign["world"] = world
    campaign["finale_cleared"] = False
    return True


def validate_structure(data):
    """校验结构合法性，返回中文错误列表（空列表 = 通过）。AI 草案与手工编辑共用。"""
    errors = []
    if not isinstance(data, dict):
        return ["顶层必须是 JSON 对象"]
    if data.get("pressure_type") not in VALID_PRESSURE_TYPES:
        errors.append("pressure_type 必须是 " + " / ".join(VALID_PRESSURE_TYPES) + " 之一")
    hub = data.get("hub")
    if not isinstance(hub, dict) or not str(hub.get("name", "")).strip():
        errors.append("hub.name（枢纽镇名）必填")
    quests = data.get("quests")
    if not isinstance(quests, list) or not 3 <= len(quests) <= 15:
        errors.append("quests 必须是 3-15 个任务的数组")
    else:
        seen_ids = set()
        for index, quest in enumerate(quests):
            if not isinstance(quest, dict):
                errors.append(f"quests[{index}] 必须是对象")
                continue
            qid = str(quest.get("id", "")).strip()
            if not qid:
                errors.append(f"quests[{index}].id 必填")
            elif qid in seen_ids:
                errors.append(f"quests[{index}].id 重复：{qid}")
            else:
                seen_ids.add(qid)
            try:
                tier = int(quest.get("tier"))
            except (TypeError, ValueError):
                tier = 0
            if tier not in (1, 2, 3):
                errors.append(f"quests[{index}].tier 必须是 1-3")
            for field in ("title", "location", "objective", "completion"):
                if not str(quest.get(field, "")).strip():
                    errors.append(f"quests[{index}].{field} 必填")
            status = quest.get("status")
            if status is not None and status not in VALID_STATUS:
                errors.append(f"quests[{index}].status 非法：{status}")
    finale = data.get("finale")
    if (
        not isinstance(finale, dict)
        or not str(finale.get("location", "")).strip()
        or not str(finale.get("objective", "")).strip()
    ):
        errors.append("finale.location 与 finale.objective 必填")
    pressure = data.get("pressure")
    if isinstance(pressure, dict):
        max_value = pressure.get("max")
        try:
            max_value = int(max_value)
        except (TypeError, ValueError):
            errors.append("pressure.max 必须是整数")
            max_value = None
        if max_value is not None and max_value < 1:
            errors.append("pressure.max 必须 ≥ 1")
        if max_value is not None and isinstance(quests, list) and len(quests) > 0 and max_value <= len(quests):
            errors.append(
                f"pressure.max（{max_value}）必须大于任务数（{len(quests)}），"
                f"否则任务全清时必然满格、结局路线被锁死；建议 {len(quests) + 1}"
            )
        if max_value is not None:
            try:
                level = int(pressure.get("level"))
            except (TypeError, ValueError):
                errors.append("pressure.level 必须是整数")
                level = None
            if level is not None and not 0 <= level <= max_value:
                errors.append(f"pressure.level 必须在 0-{max_value} 之间")
    return errors


def level_target(campaign):
    """
    里程碑目标等级（对齐官团节奏，等级上限 20）：
    - 初始任务（1 阶）：每完成 1 项 +1 级，直到 3 级（第 3 项不再给升级）
    - 后续任务（2/3 阶）：每完成 2 项 +1 级
    - 终局（击杀/驱离白龙）：额外 +1 级
    v0.4.0 起不再封顶 6：9 任务短战役自然收尾于 6-7 级，更长战役会继续攀升。
    """
    quests = campaign.get("quests") or []
    init_done = sum(
        1 for q in quests if str(q.get("tier")) == "1" and q.get("status") == "completed"
    )
    follow_done = sum(
        1 for q in quests if str(q.get("tier")) in ("2", "3") and q.get("status") == "completed"
    )
    target = 1 + min(init_done, 2) + follow_done // 2
    if campaign.get("finale_cleared"):
        target += 1
    return min(20, target)


def quest_progress(campaign):
    """计算各阶完成情况、解锁状态与终局开启条件。"""
    quests = campaign.get("quests") or []
    tiers = {}
    for tier in (1, 2, 3):
        tier_quests = [q for q in quests if str(q.get("tier")) == str(tier)]
        completed = sum(1 for q in tier_quests if q.get("status") == "completed")
        tiers[tier] = {"total": len(tier_quests), "completed": completed}
    tiers[1]["unlocked"] = True
    for tier in (2, 3):
        previous = tiers[tier - 1]
        tiers[tier]["unlocked"] = previous["total"] > 0 and previous["completed"] >= 2
    pressure = campaign.get("pressure") or {}
    try:
        pressure_full = int(pressure.get("level") or 0) >= int(pressure.get("max") or 6)
    except (TypeError, ValueError):
        pressure_full = False
    finale_unlocked = (tiers[3]["total"] > 0 and tiers[3]["completed"] >= 2) or pressure_full
    return {"tiers": tiers, "pressure_full": pressure_full, "finale_unlocked": finale_unlocked}


def find_quest(campaign, query):
    """按 id 或标题找任务；找不到时退化为包含匹配（AI 常写成"前往 XX 矿洞"）。"""
    key = str(query or "").strip().lower()
    if not key:
        return None
    quests = campaign.get("quests") or []
    for quest in quests:
        if str(quest.get("id", "")).lower() == key or str(quest.get("title", "")).lower() == key:
            return quest
    for quest in quests:
        if key in str(quest.get("title", "")).lower() or key in str(quest.get("id", "")).lower():
            return quest
    return None


def _matches_finale(campaign, query):
    """query 是否指向"终局"（支持写：终局 / finale / 终局地点名 / 终局目标）。"""
    key = str(query or "").strip().lower()
    if not key:
        return False
    finale = campaign.get("finale") or {}
    candidates = ["终局", "finale", str(finale.get("location", "")), str(finale.get("objective", ""))]
    for candidate in candidates:
        text = candidate.strip().lower()
        if text and (key == text or key in text or text in key):
            return True
    return False


def update_quest(campaign, query, status, note=""):
    """
    更新任务状态（AI 工具入口）。返回中文摘要：
    - 完成任务自动推进压力一格（threat_clock）
    - 目标等级上升时播报里程碑（官团节奏，见 level_target）
    - query 指向"终局"时视为击杀/驱离白龙：标记 finale_cleared（额外 +1 级）
    非法输入抛 ValueError（由工具层转成给 AI 的提示文本）。
    """
    if status not in VALID_STATUS:
        raise ValueError(f"非法状态：{status}（可选：{'/'.join(VALID_STATUS)}）")
    before_target = level_target(campaign)
    quest = find_quest(campaign, query)
    if not quest:
        if status == "completed" and _matches_finale(campaign, query):
            already = bool(campaign.get("finale_cleared"))
            campaign["finale_cleared"] = True
            after_target = level_target(campaign)
            parts = ["终局达成——白龙之患终结" + ("（此前已标记过）" if already else "")]
            if after_target > before_target:
                parts.append(f"里程碑达成：队伍应升到 {after_target} 级——为每个角色（含同伴）调用 level_up")
            parts.append("可进入战役收尾：按抉择史演绎多结局")
            return "；".join(parts)
        raise ValueError(f"找不到任务：{query}")
    old_status = quest.get("status", "undiscovered")
    quest["status"] = status
    if note:
        quest.setdefault("notes", []).append(str(note)[:300])
    parts = [f"任务「{quest.get('title', query)}」状态：{old_status} → {status}"]
    if status == "completed" and old_status != "completed":
        pressure = campaign.get("pressure") or {}
        if campaign.get("pressure_type") == "threat_clock":
            try:
                max_value = int(pressure.get("max") or 0)
            except (TypeError, ValueError):
                max_value = 0
            if max_value > 0:
                pressure["level"] = min(int(pressure.get("level") or 0) + 1, max_value)
                parts.append(f"压力「{pressure.get('label', '威胁')}」推进到 {pressure['level']}/{max_value}")
    # 里程碑：目标等级较更新前上升时提示升级（初始任务到 3 级 / 后续每 2 项 / 终局+1）
    after_target = level_target(campaign)
    if after_target > before_target:
        parts.append(f"里程碑达成：队伍应升到 {after_target} 级——为每个角色（含同伴）调用 level_up")
    progress = quest_progress(campaign)
    tiers = progress["tiers"]
    tier_txt = "；".join(
        f"{tier}阶 {tiers[tier]['completed']}/{tiers[tier]['total']}"
        + ("" if tier == 1 else ("（已解锁）" if tiers[tier]["unlocked"] else "（未解锁）"))
        for tier in (1, 2, 3)
    )
    parts.append("进度：" + tier_txt)
    if progress["finale_unlocked"]:
        parts.append("终局条件已满足，可引导玩家前往终局地点（击杀/驱离白龙后用 update_quest(终局, completed) 标记）")
    if progress["pressure_full"]:
        parts.append("压力已满格——应尽快把剧情推向终局事件")
    return "；".join(parts)


def add_world_note(campaign, kind, name, note):
    """登记/更新一条世界笔记（地点/NPC/线索）。同名条目做追加而非新建。"""
    if kind not in NOTE_KINDS:
        raise ValueError(f"kind 必须是 {'/'.join(NOTE_KINDS)} 之一")
    name = str(name or "").strip()[:60]
    note = str(note or "").strip()[:300]
    if not name or not note:
        raise ValueError("需要 name 与 note")
    world = campaign.setdefault("world", {})
    entries = world.setdefault(kind, [])
    for entry in entries:
        if entry.get("name") == name:
            merged = (entry.get("note", "") + "；" + note)[:600]
            entry["note"] = merged
            entry["ts"] = time.time()
            return f"已更新{NOTE_LABELS[kind]}「{name}」"
    entries.append({"name": name, "note": note, "ts": time.time()})
    if len(entries) > 200:
        del entries[: len(entries) - 200]
    return f"已登记{NOTE_LABELS[kind]}「{name}」"


def advance_pressure(campaign, amount=1, reason=""):
    """手动推进压力条（AI 用于"玩家无视代价/世界恶化"的情形）。返回播报文本。"""
    if campaign.get("pressure_type") != "threat_clock":
        raise ValueError("当前模组的压力机制不是进度条（pressure_type 非 threat_clock）")
    pressure = campaign.get("pressure") or {}
    try:
        max_value = int(pressure.get("max") or 10)
    except (TypeError, ValueError):
        max_value = 10
    try:
        amount = int(amount or 1)
    except (TypeError, ValueError):
        amount = 1
    amount = max(1, min(amount, 6))
    old_level = int(pressure.get("level") or 0)
    pressure["level"] = min(old_level + amount, max_value)
    text = f"压力「{pressure.get('label', '威胁')}」{old_level} → {pressure['level']}/{max_value}"
    if reason:
        text += f"（{reason}）"
    if pressure["level"] >= max_value:
        text += "；压力已满格——终局条件触发，尽快将剧情推向终局地点"
    return text


STATUS_MARK = {
    "in_progress": "●",
    "undiscovered": "○",
    "completed": "✓",
    "failed": "✗",
    "expired": "−",
}
STATUS_LABEL = {
    "in_progress": "进行中",
    "undiscovered": "未发现",
    "completed": "已完成",
    "failed": "失败",
    "expired": "过期",
}
TIER_ZH = {1: "一阶任务", 2: "二阶任务", 3: "三阶任务"}


def render_dm_brief(campaign):
    """
    GM 秘闻（人类 GM 阅读版，与 AI 提示词版 render_brief 分开维护）：
    分阶分组、状态标记、进行中/未发现任务展开全字段（含剧透），
    已完成/失败/过期任务压缩为单行；末尾附终局与世界登记。
    """
    if not campaign:
        return ""
    lines = []
    pressure = campaign.get("pressure") or {}
    progress = quest_progress(campaign)
    tiers = progress["tiers"]
    completed_total = sum(tiers[t]["completed"] for t in (1, 2, 3))
    total = sum(tiers[t]["total"] for t in (1, 2, 3))
    lines.append(f"【GM 秘闻】《{campaign.get('title', '未命名')}》")
    if campaign.get("theme"):
        lines.append(f"主题：{campaign['theme']}")
    lines.append(
        f"压力：「{pressure.get('label', '威胁')}」{pressure.get('level', 0)}/{pressure.get('max', 6)}"
        f" ｜ 任务 {completed_total}/{total} 完成"
        + (" ｜ 终局已开启" if progress["finale_unlocked"] else " ｜ 终局未开启")
    )
    hub = campaign.get("hub") or {}
    lines.append(f"枢纽：{hub.get('name', '')}（{str(hub.get('desc', ''))[:60]}）· 发布人：{hub.get('giver', '')}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")

    quests = sorted(
        campaign.get("quests") or [],
        key=lambda q: (int(q.get("tier", 1) or 1), str(q.get("id", ""))),
    )
    current_tier = None
    for quest in quests:
        tier = int(quest.get("tier", 1) or 1)
        if tier != current_tier:
            current_tier = tier
            unlocked = tiers.get(tier, {}).get("unlocked", tier == 1)
            suffix = "" if unlocked else "（未解锁）"
            lines.append("")
            lines.append(f"── {TIER_ZH.get(tier, f'{tier} 阶任务')}{suffix} " + "─" * 12)
        status = quest.get("status", "undiscovered")
        mark = STATUS_MARK.get(status, "?")
        status_label = STATUS_LABEL.get(status, status)
        head = f"{mark} {quest.get('id', '')} {quest.get('title', '')}（{status_label}）"
        if status in ("completed", "failed", "expired"):
            # 已结束的任务压缩为一行：取最近一条注记做摘要，注明总数
            notes = quest.get("notes") or []
            if notes:
                latest = str(notes[-1]).replace("\n", " ")[:80]
                head += f" · 注记：{latest}" + (f"（共 {len(notes)} 条）" if len(notes) > 1 else "")
            lines.append(head)
            continue
        lines.append(head)
        lines.append(f"   地点：{quest.get('location', '')} · 发布人：{quest.get('giver', '')}")
        lines.append(f"   目标：{quest.get('objective', '')}")
        lines.append(f"   钩子：{quest.get('hook', '')}")
        lines.append(f"   真相：{quest.get('secret', '')}")
        lines.append(f"   遭遇：{quest.get('encounters', '')}")
        lines.append(f"   完成：{quest.get('completion', '')}")
        if quest.get("fail"):
            lines.append(f"   失败：{quest.get('fail')}")
        for note in quest.get("notes") or []:
            lines.append(f"   注记：{note}")

    finale = campaign.get("finale") or {}
    if finale:
        if campaign.get("finale_cleared"):
            state = "已达成"
        elif progress["finale_unlocked"]:
            state = "已开启"
        else:
            state = "未开启"
        lines.append("")
        lines.append(f"── 终局（{state}） " + "─" * 12)
        lines.append(f"   地点：{finale.get('location', '')}")
        lines.append(f"   目标：{finale.get('objective', '')}")
        lines.append(f"   开启条件：{finale.get('condition', '')}")
        for ending in finale.get("endings") or []:
            lines.append(f"   · {ending}")

    world = campaign.get("world") or {}
    if any(world.get(kind) for kind in NOTE_KINDS):
        lines.append("")
        lines.append("── 世界登记 " + "─" * 12)
        for kind in NOTE_KINDS:
            entries = world.get(kind) or []
            if not entries:
                continue
            lines.append(f"〔{NOTE_LABELS[kind]}〕")
            for entry in entries:
                lines.append(f"   · {entry.get('name', '')}：{entry.get('note', '')}")
    return "\n".join(lines)


def render_brief(campaign):
    """
    渲染给 AI 的"战役结构"段落（含剧透字段，仅进系统提示词与 get_campaign）。
    总长控制在 4000 字符内，超出时截断世界笔记部分。
    """
    if not campaign:
        return ""
    lines = []
    pressure = campaign.get("pressure") or {}
    lines.append(
        f"【战役结构（硬约束：任务的地点/目标/关键转折必须与之一致；与主机粘贴的设定冲突时，以本结构为准）】"
    )
    lines.append(
        f"模组：{campaign.get('title', '未命名')}（{campaign.get('theme', '')}）；"
        f"压力机制：{campaign.get('pressure_type', '')}「{pressure.get('label', '威胁')}」"
        f"{pressure.get('level', 0)}/{pressure.get('max', 6)}"
    )
    hub = campaign.get("hub") or {}
    lines.append(f"枢纽镇：{hub.get('name', '')}（{hub.get('desc', '')}）；任务发布人：{hub.get('giver', '')}")
    progress = quest_progress(campaign)
    tiers = progress["tiers"]
    lines.append(
        "解锁进度："
        + "；".join(
            f"{tier}阶 {tiers[tier]['completed']}/{tiers[tier]['total']}"
            + ("" if tier == 1 else ("已解锁" if tiers[tier]["unlocked"] else "未解锁"))
            for tier in (1, 2, 3)
        )
        + ("；终局已开启" if progress["finale_unlocked"] else "")
    )
    lines.append("任务清单：")
    for quest in sorted(campaign.get("quests") or [], key=lambda q: (str(q.get("tier")), str(q.get("id")))):
        line = (
            f"- {quest.get('id', '')} [第{quest.get('tier', '?')}阶/{quest.get('status', '')}] "
            f"{quest.get('title', '')}｜地点：{quest.get('location', '')}｜发布人：{quest.get('giver', '')}｜"
            f"目标：{quest.get('objective', '')}｜hook：{quest.get('hook', '')}｜secret：{quest.get('secret', '')}｜"
            f"遭遇：{quest.get('encounters', '')}｜完成：{quest.get('completion', '')}"
        )
        if quest.get("fail"):
            line += f"｜失败：{quest.get('fail')}"
        lines.append(line)
    finale = campaign.get("finale") or {}
    lines.append(
        f"终局：{finale.get('location', '')}——{finale.get('objective', '')}"
        f"（条件：{finale.get('condition', '第三阶完成至少 2 个任务或压力满格')}；"
        f"结局倾向：{'/'.join(finale.get('endings') or [])}）"
    )
    world = campaign.get("world") or {}
    note_lines = []
    for kind in NOTE_KINDS:
        for entry in (world.get(kind) or [])[:30]:
            note_lines.append(f"- {NOTE_LABELS[kind]}：{entry.get('name', '')}——{entry.get('note', '')}")
    if note_lines:
        lines.append("世界登记（运行时笔记，可用 add_world_note 追加）：")
        lines.extend(note_lines)
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "…[已截断，完整结构可用 get_campaign 查看]"
    return text


def public_view(campaign):
    """
    给浏览器的公开视图（任务板用）：剥离 secret、未发现的任务与终局限定信息。
    """
    if not campaign:
        return None
    quests = []
    for quest in campaign.get("quests") or []:
        if quest.get("status", "undiscovered") == "undiscovered":
            continue    # 未发现的任务不下发（防剧透）
        quests.append(
            {
                "id": quest.get("id"),
                "tier": quest.get("tier"),
                "title": quest.get("title"),
                "location": quest.get("location"),
                "giver": quest.get("giver"),
                "objective": quest.get("objective"),
                "hook": quest.get("hook"),
                "status": quest.get("status"),
                "notes": quest.get("notes") or [],
            }
        )
    pressure = campaign.get("pressure") or {}
    progress = quest_progress(campaign)
    finale = campaign.get("finale") or {}
    return {
        "title": campaign.get("title"),
        "theme": campaign.get("theme"),
        "pressure_type": campaign.get("pressure_type"),
        "pressure": {
            "label": pressure.get("label"),
            "max": pressure.get("max"),
            "level": pressure.get("level"),
        },
        "hub": campaign.get("hub"),
        "quests": quests,
        "finale_cleared": bool(campaign.get("finale_cleared")),
        "finale": (
            {
                "location": finale.get("location"),
                "objective": finale.get("objective"),
                "endings": finale.get("endings"),
            }
            if progress["finale_unlocked"]
            else None
        ),
    }


def merge_campaign(existing, new):
    """
    保存结构时的合并策略：保留运行时状态，避免主机编辑结构导致进度丢失。
    - 世界登记（world）整体保留
    - 同 id 任务保留 status 与 notes
    - 压力类型不变时保留 level
    """
    if not existing:
        return normalize_structure(new)
    new = normalize_structure(new)
    new["world"] = existing.get("world") or new.get("world") or {
        "locations": [],
        "npcs": [],
        "clues": [],
    }
    # 终局达成标记不可被编辑结构时抹掉
    new["finale_cleared"] = bool(existing.get("finale_cleared") or new.get("finale_cleared"))
    old_quests = {str(q.get("id")): q for q in existing.get("quests") or []}
    for quest in new.get("quests") or []:
        old = old_quests.get(str(quest.get("id")))
        if old:
            quest["status"] = old.get("status", quest.get("status", "undiscovered"))
            if old.get("notes"):
                quest["notes"] = old["notes"]
    if existing.get("pressure_type") == new.get("pressure_type"):
        old_pressure = existing.get("pressure") or {}
        new_pressure = new.get("pressure") or {}
        try:
            new_pressure["level"] = min(int(old_pressure.get("level") or 0), int(new_pressure.get("max") or 6))
        except (TypeError, ValueError):
            pass
    return new
