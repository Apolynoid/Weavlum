"""
房间状态与持久化：整个进程唯一一份"当前战役"的内存模型，以及战斗规则执行。

内容分四块：
  1. 设置读写（data/settings.json）——LLM 配置
  2. RoomState 类——角色、参与者、日志、世界设定、AI 摘要、战斗状态；
     角色创建/导入时会自动写入 data/characters/（防丢），其余靠手动存档
  3. 战役存档——手动保存/列出/读取 data/saves/*.json
  4. 战斗规则——开战掷先攻、回合流转、移动（速度截断/回合校验）、
     伤害结算、怪物生成（含同名编号）、角色战斗中入场

设计约定：本模块是"战役规则执行器"，只接收普通参数、返回结果或抛中文 ValueError；
网络广播由 main.py 负责，LLM 相关由 ai.py 负责。
"""

import json
import re
import time
from pathlib import Path

from . import campaign as campaign_mod, characters, debuglog, engine, srd
from . import spells as spells_mod

# 战术板逻辑尺寸（单位：尺）。位置全部以"尺"存储，前端按比例换算像素。
BOARD_W = 120.0
BOARD_H = 60.0

# 运行期数据目录（角色、存档、设置都放这里；settings/存档已在 .gitignore 中）
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SAVE_DIR = DATA_DIR / "saves"
CHAR_DIR = DATA_DIR / "characters"
SETTINGS_FILE = DATA_DIR / "settings.json"
# 长驻配置类状态（世界设定/主持风格/摘要/战役结构）：随每次修改自动落盘，重启不丢
ROOM_META_FILE = DATA_DIR / "room_meta.json"

# 默认 LLM 设置（DeepSeek 官方接口；可换任意 OpenAI 兼容端点，如本地 Ollama）
DEFAULT_SETTINGS = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
    }
}


def load_settings():
    """读取 settings.json 并与默认值合并（文件不存在/损坏都回落到默认值）。"""
    data = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    # 深拷贝默认值再覆盖，避免调用方篡改 DEFAULT_SETTINGS
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    if isinstance(data.get("llm"), dict):
        merged["llm"].update(data["llm"])
    return merged


def save_settings(settings):
    """把 LLM 设置写回 settings.json（含 API key，明文存本机，局域网自用可接受）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def public_character(char):
    """对外广播用的角色卡：剥掉 owner_sid（会话凭据），附带负重计算（界面提示用）。"""
    out = dict(char)
    out.pop("owner_sid", None)
    out["carry"] = characters.carry_info(char)
    return out


def format_clock(minutes):
    """游戏内时间格式化：'第 N 天 HH:MM'（战斗每轮 6 秒，长休 8 小时等由引擎推进）。"""
    total = int(minutes or 0)
    day = total // 1440 + 1
    hour = (total % 1440) // 60
    minute = total % 60
    return f"第 {day} 天 {hour:02d}:{minute:02d}"


def briefing_text(room, viewer_sid=None):
    """
    进入冒险时的"当前状态"系统简报（引擎生成，仅发给刚接入的玩家本人）：
    战役/时间 · 队伍 · 任务与压力 · 战斗 · 上次进展。新开或重新加入都会收到。
    只读取玩家可见字段（不包含任务 secret 等剧透）；GM 额外提示其专属视图。
    """
    lines = []
    title = (room.campaign or {}).get("title") if room.campaign else None
    lines.append(f"【当前状态】{title + ' · ' if title else ''}{format_clock(room.clock_minutes)}")
    if viewer_sid is not None and room.dm_sid == viewer_sid:
        lines.append("（你是本场 GM——右栏有 GM 控制台与含剧透的「GM 秘闻」）")
    seated = [
        c for c in room.characters.values()
        if c.get("owner_sid") or c.get("companion")
    ]
    if seated:
        parts = []
        for char in seated:
            tag = "·同伴" if char.get("companion") else ""
            parts.append(
                f"{char.get('name', '')}（{char.get('race', '')}{char.get('klass', '')} "
                f"{char.get('level', 1)}级{tag}）"
            )
        lines.append("队伍：" + "、".join(parts))
    else:
        lines.append("队伍：（暂无上桌角色）")
    campaign = room.campaign or {}
    if campaign:
        quests = campaign.get("quests") or []
        active = [q for q in quests if q.get("status") == "in_progress"]
        if active:
            quest_txt = "；".join(
                f"{q.get('title', '')}（目标：{str(q.get('objective', ''))[:24]}）" for q in active[:3]
            )
        elif any(q.get("status") != "undiscovered" for q in quests):
            quest_txt = "当前没有进行中的任务"
        else:
            quest_txt = "尚未接取任务（与发布人或 NPC 交谈获取线索）"
        pressure = campaign.get("pressure") or {}
        if campaign.get("pressure_type") == "threat_clock" and pressure.get("max"):
            quest_txt += f"；压力「{pressure.get('label', '威胁')}」{pressure.get('level', 0)}/{pressure.get('max')}"
        lines.append("任务：" + quest_txt)
    combat = room.combat
    if combat.get("active") and combat.get("combatants"):
        combatants = combat["combatants"]
        turn = combat.get("turn", 0)
        current = combatants[turn] if 0 <= turn < len(combatants) else None
        monsters = [c for c in combatants if c.get("kind") == "monster"]
        line = (
            f"战斗进行中：第 {combat.get('round', 1)} 轮，"
            f"当前行动者：{current.get('name') if current else '—'}"
        )
        if monsters:
            line += "；敌人：" + "、".join(
                f"{c.get('name', '')}（HP {c.get('hp')}/{c.get('hp_max')}）" for c in monsters[:6]
            )
        lines.append(line)
    last_narrate = None
    for entry in reversed(room.log):
        if entry.get("kind") == "narrate":
            last_narrate = entry
            break
    if last_narrate:
        snippet = str(last_narrate.get("text", "")).replace("\n", " ")
        more = "…" if len(snippet) > 80 else ""
        lines.append(f"上次进展：{snippet[:80]}{more}")
    elif room.summary.strip():
        # 日志为空（例如服务器刚重启）时，回退到持久化的剧情摘要
        snippet = room.summary.strip().replace("\n", " ")
        more = "…" if len(snippet) > 80 else ""
        lines.append(f"剧情梗概：{snippet[:80]}{more}")
    else:
        lines.append("冒险刚刚开始——在下方输入框描述你的行动，AI GM 会为你主持（掷骰与判定由引擎执行）。")
    if room.dm_sid:
        lines.append("（当前由人类 GM 主持）")
    elif not room.ai_mode:
        lines.append("（当前无人主持——可在欢迎页或桌面接任 GM，管理员也可切回 AI 主持）")
    return "\n".join(lines)


# 玩家颜色调色板（服务端校验；名字与战术板棋子用色都从这里取，杜绝任意字符串注入）
PLAYER_COLORS = (
    "#e06c75", "#e5a44f", "#e5c74f", "#8fbf5f", "#4fb38a", "#4f9fd8", "#7a6fe0",
    "#c46fd8", "#d86f9f", "#6fb0c9", "#bf8f5f", "#9fc94f", "#d8936f", "#a8a29a",
)


# v0.5.1：规则集切换为 BFRD（黑旗）后，旧 SRD 种族 → 血统的迁移建议表
# （无完美对应者给建议值：半精灵→人类、龙裔/提夫林→兽裔；玩家可在角色卡/后续向导中手改）
LEGACY_RACE_MAP = {
    "human": "human", "elf": "elf", "dwarf": "dwarf",
    "halfling": "smallfolk", "gnome": "smallfolk",
    "half_elf": "human", "half_orc": "orc",
    "dragonborn": "beastkin", "tiefling": "beastkin",
}


def _migrate_spell_buckets(char):
    """
    旧单一法术列表 → 分层栏位（v0.6.4）：戏法归戏法栏；
    其余按职业结构归主栏位（法师→法术书，准备列表按容量截取；已知型→已知；准备型→准备）。
    仪式栏留空（旧列表中的仪式法术仍在主栏位可正常施放；仪式施法按栏位走）。
    """
    class_id = str(char.get("class_id") or "")
    entries = []
    for s in char.get("spells") or []:
        if isinstance(s, dict) and s.get("id"):
            entries.append({"id": s["id"], "name": s.get("name", s["id"]), "level": int(s.get("level", 0) or 0)})
    for key in ("cantrips", "known", "prepared", "spellbook", "rituals"):
        char.setdefault(key, [])
    char.setdefault("spellbook_credits", 0)
    char.setdefault("swap_credits", 0)
    track = spells_mod.caster_track(class_id) if class_id else None
    if not track:
        char["spell_model"] = 1
        return
    for rec in entries:
        if rec["level"] == 0 and track["cantrips"]:
            char["cantrips"].append(rec)
        elif track["spellbook"]:
            char["spellbook"].append(rec)
        elif track["known"]:
            char["known"].append(rec)
        else:
            char["prepared"].append(rec)
    if track["spellbook"]:
        try:
            prepared_cap = spells_mod.limits_for(class_id, char.get("level", 1), char)["prepared"]
        except ValueError:
            prepared_cap = len(char["spellbook"])
        ordered = sorted(char["spellbook"], key=lambda e: (-e["level"], e["name"]))
        char["prepared"] = [dict(e) for e in ordered[:prepared_cap]]
        expected = 6 + 2 * (int(char.get("level", 1)) - 1)
        char["spellbook_credits"] = max(0, expected - len(char["spellbook"]))
    char["spell_model"] = 2
    spells_mod.rebuild_spell_union(char)


def migrate_character_fields(char):
    """
    兼容旧角色卡字段：
      - spell（施法数值块）→ spellcasting（消歧：spells 是已知法术列表）
      - 背包系统：老卡没有 items 时把出身装备转成背包条目
      - 四级货币：老的单一金币数值（gold_gp）折算为 pp/gp/sp/cp 四格并移除旧字段
      - v0.5.1 BFRD 迁移：旧种族 → 血统（建议映射表）、补 talents/luck/heritage 字段
    """
    if not isinstance(char, dict):
        return
    if "spell" in char and "spellcasting" not in char:
        char["spellcasting"] = char.pop("spell")
    char.setdefault("gender", "")
    char.setdefault("pending_asi", 0)
    if not isinstance(char.get("items"), list):
        char["items"] = characters.equipment_to_items(char.get("equipment") or [])
    if not isinstance(char.get("coins"), dict):
        try:
            total_cp = int(round(float(char.get("gold_gp") or 0) * 100))
        except (TypeError, ValueError):
            total_cp = 0
        char["coins"] = characters.coins_from_cp(total_cp)
    char.pop("gold_gp", None)
    # ---- BFRD 迁移 ----
    char.setdefault("talents", [])
    char.setdefault("luck", 0)
    char.setdefault("heritage", "")
    char.setdefault("heritage_id", "")
    char.setdefault("subclass", "")
    char.setdefault("subclass_name", "")
    char.setdefault("pending_subclass", False)
    char.setdefault("pending_boons", [])
    char.setdefault("boons", [])
    # ---- v0.6.6 自然适应（兽裔/小族）----
    char.setdefault("adaptation", "")
    char.setdefault("adaptation_spell_ability", "")
    char.setdefault("alt_speed", "")
    # ---- v0.6.1 濒死/力竭/Downtime 字段 ----
    char.setdefault("exhaustion", 0)
    char.setdefault("dying", False)
    char.setdefault("stable", False)
    char.setdefault("dead", False)
    char.setdefault("death_saves", {"successes": 0, "failures": 0})
    char.setdefault("stable_until", None)
    hp = char.get("hp") or {}
    char.setdefault("hp_max_base", int(hp.get("max") or 1))
    char.setdefault("speed_base", int(char.get("speed") or 30))
    char.setdefault("contacts", 0)
    char.setdefault("favors", 0)
    char.setdefault("clues", 0)
    char.setdefault("crafting", None)
    char.setdefault("training", None)
    # ---- v0.6.4 法术三分层迁移（戏法/已知或准备/法术书/仪式）----
    if char.get("spells") is not None and "spell_model" not in char:
        _migrate_spell_buckets(char)
    # ---- v0.6.28 法术书是法师专属：旧存档里的非法师角色也一并清掉 ----
    if char.get("class_id"):
        track = spells_mod.caster_track(str(char.get("class_id")))
        if not track or not track.get("spellbook"):
            char.pop("spellbook", None)
            char.pop("spellbook_credits", None)
    # ---- v0.6.4 生命骰池 ----
    char.setdefault("hit_dice_used", 0)
    race_id = str(char.get("race_id") or "")
    if race_id and race_id not in srd.races():
        mapped = LEGACY_RACE_MAP.get(race_id)
        if mapped and mapped in srd.races():
            char["race_id"] = mapped
            char["race"] = srd.races()[mapped]["name"]


class RoomState:
    """
    单房间（单战役）的全部内存状态。

    字段说明：
      characters   角色卡 {id: char}
      participants 参与者 {sid: {nickname, connected, char_id, is_host}}
      log          冒险日志（chat/roll/narrate/system 四种 kind）
      world        主机粘贴的世界设定/模组文本
      dm_note      主机写给 AI 的额外主持要求
      summary      AI 的滚动剧情摘要（长战役记忆）
      ai_mode      总闸：无 GM 席位时是否由 AI 主持（True=AI，False=静默等玩家接任）
      dm_sid       人类 GM 的会话 id（玩家自助接任，先到先得；席位占用期间他人不可选）
      combat       战斗状态 {active, round, turn, combatants[]}
      seq          自增序号（用于生成 e/c/m 前缀的 id）
    """

    def __init__(self):
        self.characters = {}
        self.participants = {}
        self.player_profiles = {}   # 玩家档案 {昵称: {color, avatar}}：颜色/头像按昵称持久化
        self.log = []
        self.world = ""
        self.dm_note = ""
        self.summary = ""
        self.campaign = None    # 战役结构（server/campaign.py 的数据模型），None=未设置
        self.ai_mode = True
        self.dm_sid = None
        self.combat = {"active": False, "round": 0, "turn": 0, "combatants": []}
        self.seq = 0
        self.last_summary_len = 0
        self.clock_minutes = 0.0    # 游戏时钟（分钟）：每战斗轮 +0.1；advance_time/休息推进
        self._load_character_files()
        self._load_room_meta()
        self.migrate_effects()

    def migrate_effects(self):
        """兼容旧数据：把按"轮"计的效果换算成按游戏时钟的绝对到期时间（1 轮 = 0.1 分钟）。"""
        for char in self.characters.values():
            changed = False
            for effect in list(char.get("effects") or []):
                if "rounds" in effect and "expires_at" not in effect:
                    rounds = int(effect.pop("rounds", 0) or 0)
                    effect["expires_at"] = (
                        round(self.clock_minutes + rounds * 0.1, 2) if rounds > 0 else None
                    )
                    changed = True
            if changed:
                self.persist_character(char)

    def _load_room_meta(self):
        """
        启动时恢复"长驻配置类"状态：世界设定 / 主持风格 / 剧情摘要 / 战役结构 / 主持模式与 GM 席位。
        这些数据随每次修改自动落盘（persist_room_meta），重启不丢失；
        日志、战斗等对局状态仍按"手动存档"的设计管理。
        """
        if not ROOM_META_FILE.exists():
            return
        try:
            data = json.loads(ROOM_META_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.world = str(data.get("world", "") or "")
        self.dm_note = str(data.get("dm_note", "") or "")
        self.summary = str(data.get("summary", "") or "")
        try:
            self.clock_minutes = float(data.get("clock_minutes") or 0.0)
        except (TypeError, ValueError):
            self.clock_minutes = 0.0
        campaign = data.get("campaign")
        self.campaign = campaign if isinstance(campaign, dict) else None
        # 主持模式与 GM 席位也随 room_meta 恢复：重启后席位仍归原 GM（其浏览器 cookie 不变即可续任）
        self.ai_mode = bool(data.get("ai_mode", True))
        dm_sid = data.get("dm_sid")
        self.dm_sid = dm_sid if isinstance(dm_sid, str) and dm_sid else None
        # 玩家档案（颜色/头像）：昵称 → {color, avatar}，重启不丢
        players = data.get("players")
        self.player_profiles = players if isinstance(players, dict) else {}

    def persist_room_meta(self):
        """把长驻配置类状态写入 room_meta.json（世界/主持风格/摘要/战役结构/游戏时钟/主持模式与 GM 席位）。"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "world": self.world,
            "dm_note": self.dm_note,
            "summary": self.summary,
            "campaign": self.campaign,
            "clock_minutes": self.clock_minutes,
            "ai_mode": self.ai_mode,
            "dm_sid": self.dm_sid,
            "players": self.player_profiles,
            "saved_at": time.time(),
        }
        ROOM_META_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def next_id(self, prefix):
        """生成带前缀的自增 id：e=日志条目，c=角色，m=战斗者。"""
        self.seq += 1
        return f"{prefix}{self.seq}"

    def add_log(self, kind, who, text, meta=None):
        """追加一条日志。kind 取值：chat/roll/narrate/system。"""
        entry = {"id": self.next_id("e"), "ts": time.time(), "kind": kind, "who": who, "text": text}
        if meta is not None:
            entry["meta"] = meta
        self.log.append(entry)
        # 内存保护：日志最多留 3000 条，超出时裁到最近 2000 条
        if len(self.log) > 3000:
            self.log = self.log[-2000:]
        # 调试模式：所有游戏事件（chat/roll/narrate/system）同步写一份到调试日志
        debuglog.log("log", kind, who=who, text=text, meta=meta)
        return entry

    def _load_character_files(self):
        """启动时从 data/characters/ 恢复角色卡（防丢的主要保险）。"""
        if not CHAR_DIR.exists():
            return
        for path in sorted(CHAR_DIR.glob("*.json")):
            try:
                char = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(char, dict) or "name" not in char:
                continue
            # 重启后所有权一律清空：谁在线谁重新选角色
            char["owner_sid"] = None
            char["owner_nick"] = None
            cid = char.get("id") or path.stem
            char["id"] = cid
            migrate_character_fields(char)
            self.characters[cid] = char
            # 让自增序号至少大于已存在的编号，避免 id 冲突
            digits = "".join(ch for ch in cid if ch.isdigit())
            if digits:
                self.seq = max(self.seq, int(digits))

    def add_character(self, char):
        """新角色入库：分配 id 并立刻落盘。"""
        char["id"] = self.next_id("c")
        self.characters[char["id"]] = char
        self.persist_character(char)
        return char

    def persist_character(self, char):
        """把单个角色卡写入 data/characters/<id>.json（每次数值变动后调用）。"""
        CHAR_DIR.mkdir(parents=True, exist_ok=True)
        (CHAR_DIR / f"{char['id']}.json").write_text(
            json.dumps(char, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def claim_dm(self, sid):
        """
        玩家自助接任 GM 席位（先到先得，欢迎页/桌面均可）：
          - 席位已被他人占用 → 拒绝（返回占用者昵称供提示）
          - 自己已是 GM → 幂等成功（刷新/重连后继续任 GM）
          - "当 GM 就不能有角色"：接任时自动放下当前角色（退出先攻条、释放所有权）
        返回 (ok, error)：ok=False 时 error 是给玩家看的拒绝原因。
        """
        p = self.participants.get(sid)
        if not p:
            return False, "请先输入昵称"
        if self.dm_sid and self.dm_sid != sid:
            holder = (self.participants.get(self.dm_sid) or {}).get("nickname") or "另一位玩家"
            return False, f"已有 GM：{holder}，等 TA 卸任后再试"
        if self.dm_sid == sid:
            return True, ""
        self.dm_sid = sid
        # 纯主持身份：放下当前角色（若在战斗中，同步移出先攻条）
        cid = p.get("char_id")
        char = self.characters.get(cid) if cid else None
        if char:
            remove_combatant(self, char["id"])
            char["owner_sid"] = None
            char["owner_nick"] = None
            self.persist_character(char)
            p["char_id"] = None
        self.persist_room_meta()
        return True, ""

    def release_dm(self, sid):
        """GM 主动卸任（仅当前 GM 本人可调用）；席位空出后按总闸决定 AI 是否接管。"""
        if self.dm_sid != sid:
            return False, "你不是当前 GM"
        self.dm_sid = None
        self.persist_room_meta()
        return True, ""

    def kick_offline_player(self, target_sid, by_sid):
        """
        GM 将离线玩家移出房间（v0.6.29）：
          - 只有当前 GM 可操作，且目标必须处于离线状态（在线玩家的连接由 WS 生命周期管理）
          - 移出参与者条目，并释放其名下角色的所有权（角色卡保留，可被重新选用）
        返回 (ok, error, entry)：成功时 entry 是写好的系统日志，由调用方广播。
        """
        if self.dm_sid != by_sid:
            return False, "只有 GM 能移出玩家", None
        if not target_sid:
            return False, "未指定玩家", None
        if target_sid == by_sid:
            return False, "不能移出自己", None
        target = self.participants.get(target_sid)
        if not target:
            return False, "该玩家不在房间", None
        if target.get("connected"):
            return False, "只能移出离线玩家", None
        gm_name = (self.participants.get(by_sid) or {}).get("nickname") or "GM"
        target_name = target.get("nickname") or "离线玩家"
        released = []
        for char in self.characters.values():
            if char.get("owner_sid") != target_sid:
                continue
            char["owner_sid"] = None
            char["owner_nick"] = None
            released.append(char.get("name") or char.get("id"))
            self.persist_character(char)
        self.participants.pop(target_sid, None)
        suffix = f"（释放角色：{'、'.join(released)}）" if released else ""
        entry = self.add_log("system", "系统", f"{gm_name} 将离线玩家 {target_name} 移出了房间{suffix}。")
        return True, "", entry

    def set_quick_rolls(self, nickname, rolls):
        """
        保存玩家自定义固定掷骰（v0.6.32）：
          - 存入玩家档案（按昵称持久化，重启/换设备后仍保留）
          - 最多 8 条；结构与表达式由调用方（HTTP 路由）校验
        返回实际保存的列表。
        """
        profile = dict(self.player_profiles.get(nickname) or {})
        profile["quick_rolls"] = [dict(row) for row in rolls[:8]]
        self.player_profiles[nickname] = profile
        self.persist_room_meta()
        return profile["quick_rolls"]

    def ensure_profile(self, nickname, color=None, avatar=None):
        """
        玩家档案（颜色/头像），按昵称持久化：
          - color 在调色板内则更新；缺失/非法时保留旧值，仍无则自动分配（优先避开在用颜色）
          - avatar 传入新文件名则更新（None 保留旧值）
        返回更新后的档案 dict。
        """
        profile = dict(self.player_profiles.get(nickname) or {})
        if color in PLAYER_COLORS:
            profile["color"] = color
        if profile.get("color") not in PLAYER_COLORS:
            in_use = {p.get("color") for p in self.participants.values() if p.get("color")}
            profile["color"] = next(
                (c for c in PLAYER_COLORS if c not in in_use),
                PLAYER_COLORS[len(self.player_profiles) % len(PLAYER_COLORS)],
            )
        if avatar:
            profile["avatar"] = avatar
        self.player_profiles[nickname] = profile
        self.persist_room_meta()
        return profile

    def sync_combat_hp(self):
        """
        战斗板上的 PC/同伴 HP 只是角色卡的镜像（v0.6.33）：
        升级、长休、法术、力竭、手动设置等都会直接改角色卡，广播前统一从角色卡回填，
        避免战斗板继续显示开战时的旧 HP / 旧上限。
        """
        for combatant in self.combat.get("combatants") or []:
            if combatant.get("kind") not in ("pc", "ally"):
                continue
            char = self.characters.get(combatant.get("id"))
            if not char:
                continue
            combatant["hp"] = int(char["hp"]["cur"])
            combatant["hp_max"] = int(char["hp"]["max"])

    def snapshot(self):
        """发给浏览器的房间快照：角色卡剥掉敏感字段，日志只带最近 200 条。"""
        self.sync_combat_hp()
        campaign_view = campaign_mod.public_view(self.campaign)
        if campaign_view:
            # 里程碑提示：按"每完成 2 个任务升 1 级"的目标等级 vs 队员当前等级
            expected_level = campaign_mod.level_target(self.campaign)
            seated_levels = [
                int(c.get("level", 1))
                for c in self.characters.values()
                if c.get("owner_sid") or c.get("companion")
            ]
            if seated_levels and max(seated_levels) < expected_level:
                campaign_view["milestone_hint"] = f"队伍应达到 {expected_level} 级——记得升级（可让 AI GM 调用 level_up）"
        return {
            "participants": [
                {
                    "sid": sid,
                    "nickname": p.get("nickname", ""),
                    "connected": bool(p.get("connected")),
                    "char_id": p.get("char_id"),
                    "is_host": bool(p.get("is_host")),
                    # is_dm：仅取决于是否持有 GM 席位（自助接任，与主持模式无关；
                    # 主机是服务器管理员，不自动拥有 GM 权限）
                    "is_dm": self.dm_sid is not None and self.dm_sid == sid,
                    # 玩家颜色/头像（名字与战术板棋子渲染用）
                    "color": p.get("color") or "",
                    "avatar": p.get("avatar") or "",
                }
                for sid, p in self.participants.items()
            ],
            "characters": {cid: public_character(c) for cid, c in self.characters.items()},
            "log": self.log[-200:],
            "combat": self.combat,
            "ai_mode": self.ai_mode,
            "dm_sid": self.dm_sid,
            "world_set": bool(self.world.strip()),
            # 游戏时钟（分钟 + 格式化展示）
            "clock": format_clock(self.clock_minutes),
            "clock_minutes": self.clock_minutes,
            # 战役结构公开视图（剥离剧透）；任务板等前端功能消费它
            "campaign": campaign_view,
        }

    def campaign_payload(self):
        """整场战役的存档结构（手动保存与导出共用同一格式）。"""
        self.sync_combat_hp()
        return {
            "format": "weavlum-campaign",
            "version": 1,
            "saved_at": time.time(),
            "world": self.world,
            "dm_note": self.dm_note,
            "summary": self.summary,
            "campaign": self.campaign,
            "ai_mode": self.ai_mode,
            "dm_sid": self.dm_sid,
            "combat": self.combat,
            "characters": self.characters,
            "log": self.log,
            "clock_minutes": self.clock_minutes,
        }

    def load_campaign(self, payload):
        """
        用存档覆盖当前房间状态。
        角色表是"完全替换"语义：不在存档里的角色会从内存与磁盘一并清除，
        避免载入后出现残影角色或 id 冲突；所有权重置，所有人需重新选角。
        """
        if payload.get("format") != "weavlum-campaign":
            raise ValueError("不是有效的战役存档")
        self.world = payload.get("world", "")
        self.dm_note = payload.get("dm_note", "")
        self.summary = payload.get("summary", "")
        self.campaign = payload.get("campaign")
        self.ai_mode = bool(payload.get("ai_mode", True))
        self.dm_sid = payload.get("dm_sid")
        self.combat = payload.get("combat") or {"active": False, "round": 0, "turn": 0, "combatants": []}
        self.log = list(payload.get("log", []))
        # 摘要从当前日志位置重新起算，避免载入后立刻触发一次重复总结
        self.last_summary_len = len(self.log)
        try:
            self.clock_minutes = float(payload.get("clock_minutes") or 0.0)
        except (TypeError, ValueError):
            self.clock_minutes = 0.0
        characters = payload.get("characters") or {}
        # 清掉当前不在存档中的角色（含磁盘文件）
        for cid in list(self.characters):
            if cid not in characters:
                self.characters.pop(cid, None)
                try:
                    (CHAR_DIR / f"{cid}.json").unlink(missing_ok=True)
                except OSError:
                    pass
        for cid, char in characters.items():
            char["id"] = cid
            char["owner_sid"] = None
            char["owner_nick"] = None
            migrate_character_fields(char)
            self.characters[cid] = char
            self.persist_character(char)
            # 让自增序号至少大于已存在的编号，避免 id 冲突
            digits = "".join(ch for ch in cid if ch.isdigit())
            if digits:
                self.seq = max(self.seq, int(digits))
        self.migrate_effects()
        # 载入后所有人需要重新选角色
        for participant in self.participants.values():
            participant["char_id"] = None


def save_campaign(room, name):
    """手动存档：写 data/saves/<name>.json。名字做文件系统安全过滤，空名用时间戳。"""
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in str(name) if ch not in '\\/:*?"<>|').strip()
    if not safe:
        safe = time.strftime("存档-%Y%m%d-%H%M%S")
    payload = room.campaign_payload()
    payload["name"] = safe
    (SAVE_DIR / f"{safe}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return safe


def list_saves():
    """列出所有存档的摘要信息（按保存时间倒序）。"""
    if not SAVE_DIR.exists():
        return []
    items = []
    for path in SAVE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        items.append(
            {
                "name": data.get("name", path.stem),
                "saved_at": data.get("saved_at"),
                "chars": len(data.get("characters", {})),
                "log": len(data.get("log", [])),
            }
        )
    return sorted(items, key=lambda item: item.get("saved_at") or 0, reverse=True)


def load_campaign_file(name):
    """按名字读取存档文件（不覆盖房间，由调用方决定何时 load_campaign）。"""
    safe = "".join(ch for ch in str(name) if ch not in '\\/:*?"<>|').strip()
    path = SAVE_DIR / f"{safe}.json"
    if not path.exists():
        raise ValueError("存档不存在")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 战斗规则执行区（全部由引擎计算，AI 与玩家走同一套函数）
# ---------------------------------------------------------------------------


def start_combat(room):
    """
    开始战斗：为每位"已上桌"的角色与同伴掷先攻并编号站位（左侧 x=15 一排），
    先攻从高到低排序，回合指针归零。玩家角色 kind=pc，同伴 kind=ally。
    """
    seated = [
        char for char in room.characters.values()
        if char.get("owner_sid") or char.get("companion")
    ]
    combatants = []
    count = len(seated)
    for index, char in enumerate(seated):
        init_mod = char["mods"]["dex"]
        roll = engine.roll_expr(f"1d20{init_mod:+d}")
        init_total = characters.initiative_total(char, roll["total"])   # 「警觉」天赋：d20 下限 10
        # 多个角色时沿纵向排开，单个角色站正中
        if count > 1:
            y = 30 + (index - (count - 1) / 2) * 12
        else:
            y = 30
        pos = {"x": 15.0, "y": max(6.0, min(54.0, y))}
        combatants.append(
            {
                "id": char["id"],
                "name": char["name"],
                "kind": "ally" if char.get("companion") else "pc",
                "init": init_total,
                "hp": char["hp"]["cur"],
                "hp_max": char["hp"]["max"],
                "speed": char.get("speed", 30),
                "pos": pos,
                "turn_start": dict(pos),   # 本回合移动起点（算已移动距离用）
                "dashing": False,          # 冲刺：本回合允许双倍移动
            }
        )
    combatants.sort(key=lambda item: item["init"], reverse=True)
    room.combat = {"active": True, "round": 1, "turn": 0, "combatants": combatants}
    return room.combat


def next_turn(room):
    """
    回合流转：清空冲刺状态，指针后移（到尾则新一轮），新行动者重置移动起点。
    每次进入新一轮时，为在场角色结算效果倒计时（到期自动清理并播报）。
    返回到期播报文本列表（由调用方写入日志与广播）。
    """
    combat = room.combat
    if not combat.get("active") or not combat.get("combatants"):
        return []
    expired = []
    for combatant in combat["combatants"]:
        combatant["dashing"] = False
    combat["turn"] += 1
    if combat["turn"] >= len(combat["combatants"]):
        combat["turn"] = 0
        combat["round"] += 1
        # 新一轮：游戏时钟推进 6 秒（1 轮 = 0.1 分钟），按时钟结算效果到期
        room.clock_minutes = round(room.clock_minutes + 0.1, 2)
        room.persist_room_meta()     # 时钟属长驻状态，随每轮落盘（重启不回退）
        for combatant in combat["combatants"]:
            if combatant.get("kind") not in ("pc", "ally"):
                continue
            char = room.characters.get(combatant.get("id"))
            if char:
                expired.extend(spells_mod.expire_by_clock(char, room.clock_minutes))
                room.persist_character(char)
    current = combat["combatants"][combat["turn"]]
    if current.get("pos"):
        current["turn_start"] = dict(current["pos"])
    # 濒死角色的回合开始：引擎自动掷死亡豁免（BFRD：d20 ≥10 成功；3 成功稳定 / 3 失败死亡）
    if current.get("kind") in ("pc", "ally"):
        char = room.characters.get(current.get("id"))
        if char and char.get("dying") and not char.get("dead"):
            roll = engine.roll_expr("1d20")
            texts, events = characters.death_save_roll(char, roll["total"], room.clock_minutes)
            if "stable" in events:
                d4 = engine.roll_expr("1d4")["total"]
                char["stable_until"] = round(room.clock_minutes + d4 * 60, 1)
                texts.append(f"{char['name']} 稳定：将于约 {d4} 小时后自行苏醒（回复 1 HP）")
            if "recover" in events:
                texts.append(f"{char['name']} 掷出 20：恢复意识（1 HP，仍倒地）")
            expired.extend(texts)
            room.persist_character(char)
            for combatant in combat["combatants"]:
                if combatant.get("id") == char["id"]:
                    combatant["hp"] = char["hp"]["cur"]
                    break
    return expired


def find_character(room, query):
    """按 id/名字（含包含匹配）在角色卡里找人；找不到返回 None。"""
    key = str(query or "").strip().lower()
    if not key:
        return None
    for char in room.characters.values():
        if char.get("id", "").lower() == key or char.get("name", "").lower() == key:
            return char
    for char in room.characters.values():
        if key in str(char.get("name", "")).lower():
            return char
    return None


def distance(a, b):
    """两点间距离（尺），战术板坐标直接用勾股定理。"""
    return ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5


def _numbered_parts(name):
    """把 "地精2" 拆成 ("地精", 2)；没有数字后缀则返回 (原名, None)。"""
    match = re.match(r"^(.*?)(\d+)$", str(name or ""))
    if match:
        return match.group(1), int(match.group(2))
    return str(name or ""), None


def _same_family(candidate_name, base):
    """判断某名字是否属于 base 的编号家族（"地精"、"地精1" 都算）。"""
    stem, _ = _numbered_parts(candidate_name)
    return stem == base


def match_combatants(combatants, query):
    """
    按 id/名字精确匹配；找不到时退化为"家族匹配"（说"地精"能命中"地精1/2"）。
    返回可能是多个（同名多只），调用方自行决定取最近还是报错要求编号。
    """
    key = str(query or "").strip()
    if not key:
        return []
    low = key.lower()
    exact = [
        c
        for c in combatants
        if c.get("id", "").lower() == low or c.get("name", "").lower() == low
    ]
    if exact:
        return exact
    return [c for c in combatants if _same_family(c.get("name", "").lower(), low)]


def move_token(room, combatant_id, x, y):
    """无条件摆放到指定坐标（坐标钳制在板面内）。权限与移动力校验在 main.py 做。"""
    for combatant in room.combat.get("combatants", []):
        if combatant.get("id") == combatant_id:
            combatant["pos"] = {
                "x": max(0.0, min(BOARD_W, float(x))),
                "y": max(0.0, min(BOARD_H, float(y))),
            }
            return combatant
    return None


def set_monster_hp(room, target_id, delta=None, value=None):
    """
    先攻条上怪物 HP 调整（GM 快捷操作）：delta=任意增量，value=设为精确值（优先）。
    HP 归零会自动清理（prune_combat，含全灭判负）。
    返回 (found, pruned_notes)：找不到该怪物时 found=False。
    """
    for combatant in room.combat.get("combatants", []):
        if combatant.get("id") == target_id and combatant.get("kind") == "monster":
            hp_max = int(combatant.get("hp_max", 1))
            if value is not None:
                new_hp = int(value)
            else:
                new_hp = int(combatant.get("hp", 0)) + int(delta or 0)
            combatant["hp"] = max(0, min(hp_max, new_hp))
            if combatant["hp"] <= 0:
                return True, prune_combat(room)
            return True, []
    return False, []


def adjust_hp(room, target_query, delta, crit=False):
    """
    HP 结算（伤害为负、治疗为正），返回中文结果摘要。

    目标可以是角色（会同步角色卡 + 战术板镜像值）或战斗中的怪物；
    同名怪物多于一只时抛错要求指定编号（如"地精2"）。

    濒死系统（BFRD）自动结算：降至 0 HP 进入濒死（挂昏迷/倒地）；
    巨量伤害（溢出≥生命上限）当场死亡；0 HP 期间再受伤自动失败一次（crit 两次）；
    回复生命值自动解除濒死；已死亡角色普通治疗无效（需要复活法术）。
    """
    key = str(target_query or "").strip().lower()
    if not key:
        raise ValueError("缺少目标")
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise ValueError("HP 变化数值无效")
    # 先找角色卡（精确 id/名字）
    char = None
    for candidate in room.characters.values():
        if candidate.get("id", "").lower() == key or candidate.get("name", "").lower() == key:
            char = candidate
            break
    if char:
        if char.get("dead"):
            return f"{char['name']} 已死亡：普通治疗与伤害无效（需要复苏术 / 死者复活 / 复活术等）"
        before = char["hp"]["cur"]
        char["hp"]["cur"] = max(0, min(char["hp"]["max"], before + delta))
        applied = char["hp"]["cur"] - before
        # 受伤时自动做专注检定（失败会立刻中断专注效果，需一并落盘）
        save_text = spells_mod.concentration_save(char, -applied) if applied < 0 else None
        room.persist_character(char)
        # 同步战术板上的镜像 HP（如果该角色正在战斗中）
        for combatant in room.combat.get("combatants", []):
            if combatant.get("id") == char["id"]:
                combatant["hp"] = char["hp"]["cur"]
                break
        if delta < 0 and before <= 0:
            # 已经在 0 HP：再受伤自动失败一次（重击两次）；稳定者先回到濒死
            if char.get("stable"):
                char["stable"] = False
                char["dying"] = True
                char["stable_until"] = None
            fail_text = characters.damage_while_dying(char, crit=bool(crit))
            room.persist_character(char)
            return f"{char['name']} 在 0 HP 状态下受到 {-delta} 点伤害（HP 保持 0）" + (f"；{fail_text}" if fail_text else "")
        if applied < 0:
            text = f"{char['name']} 受到 {-applied} 点伤害，HP {char['hp']['cur']}/{char['hp']['max']}"
            if save_text:
                text += "；" + save_text
            if char["hp"]["cur"] == 0:
                overflow = (-delta) - max(0, before)
                if overflow >= int(char["hp"]["max"]):
                    # 巨量伤害：溢出伤害 ≥ 生命值上限 → 当场死亡
                    text += "；巨量伤害（溢出 " + str(overflow) + " ≥ 上限 " + str(char["hp"]["max"]) + "）：" + characters.mark_dead(char)
                    room.persist_character(char)
                else:
                    text += "；" + characters.start_dying(char, room.clock_minutes)
            return text
        if applied > 0:
            if char.get("dying") or char.get("stable"):
                characters.stop_dying(char)
                room.persist_character(char)
                return f"{char['name']} 恢复 {applied} 点，HP {char['hp']['cur']}/{char['hp']['max']}；解除昏迷与濒死（仍倒地）"
            return f"{char['name']} 恢复 {applied} 点，HP {char['hp']['cur']}/{char['hp']['max']}"
        return f"{char['name']} 的 HP 无变化（{char['hp']['cur']}/{char['hp']['max']}）"
    # 再找战斗中的怪物
    matches = match_combatants(room.combat.get("combatants", []), target_query)
    if len(matches) > 1:
        raise ValueError("同名目标有多个，请指定编号：" + "、".join(c["name"] for c in matches))
    combatant = matches[0] if matches else None
    if not combatant:
        raise ValueError(f"找不到目标：{target_query}")
    before = combatant.get("hp", 0)
    combatant["hp"] = max(0, min(combatant.get("hp_max", 1), before + delta))
    applied = combatant["hp"] - before
    knocked = combatant["hp"] <= 0 and before > 0
    if applied < 0:
        text = f"{combatant['name']} 受到 {-applied} 点伤害，HP {combatant['hp']}/{combatant['hp_max']}"
    elif applied > 0:
        text = f"{combatant['name']} 恢复 {applied} 点，HP {combatant['hp']}/{combatant['hp_max']}"
    else:
        text = f"{combatant['name']} 的 HP 无变化（{combatant['hp']}/{combatant['hp_max']}）"
    if knocked:
        # 倒地即清理：移出先攻条（含指针校正）；场上再无怪物则自动结束战斗
        pruned = prune_combat(room)
        if pruned:
            text += "（" + "；".join(pruned) + "）"
    return text


def move_combatant_toward(room, mover_query, target_query, feet):
    """
    "向目标移动"：AI 与玩家声明移动共用的引擎入口。

    规则：
      - 直线朝目标走，最多走到距目标 5 尺（近战距离），不重叠
      - 步长被三重截断：请求尺数、本回合剩余移动力（速度×冲刺）、与目标的距离差
      - 玩家角色仅限其回合移动（怪物无此限制，由 GM/AI 控制）
    返回 (被移动的战斗者, 中文摘要)；无法再移动时返回 (None, 原因)。
    """
    combat = room.combat
    if not combat.get("active"):
        raise ValueError("当前不在战斗中")
    combatants = [c for c in combat.get("combatants", []) if c.get("pos")]
    movers = match_combatants(combatants, mover_query)
    if not movers:
        raise ValueError(f"找不到移动者：{mover_query}")
    mover_ids = {m["id"] for m in movers}
    targets = [c for c in match_combatants(combatants, target_query) if c["id"] not in mover_ids]
    if not targets:
        raise ValueError(f"找不到目标：{target_query}")
    target = targets[0]
    # 同名多只时选离目标最近的那只（最合理的默认）
    mover = min(movers, key=lambda m: distance(m["pos"], target["pos"]))
    if mover.get("kind") in ("pc", "ally"):
        all_combatants = combat.get("combatants") or []
        turn = combat.get("turn", 0)
        current = all_combatants[turn] if 0 <= turn < len(all_combatants) else None
        if not current or current.get("id") != mover["id"]:
            current_name = current["name"] if current else "—"
            raise ValueError(
                f"还没轮到 {mover['name']} 行动（当前行动者：{current_name}）。应先调用 advance_turn 使先攻条轮转到位。"
            )
    start = mover.get("turn_start") or mover["pos"]
    allowed = float(mover.get("speed", 30)) * (2 if mover.get("dashing") else 1)
    remaining = max(0.0, allowed - distance(start, mover["pos"]))
    gap = distance(mover["pos"], target["pos"])
    try:
        step = max(0.0, float(feet))
    except (TypeError, ValueError):
        step = 30.0
    step = min(step, remaining, max(0.0, gap - 5.0))
    if step <= 0.01:
        summary = (
            f"{mover['name']} 无法再向 {target['name']} 移动：剩余移动力 {remaining:.0f} 尺，距目标 {gap:.0f} 尺"
        )
        return None, summary
    # 沿单位向量前进 step 尺，再钳制在板面范围内
    dx = (target["pos"]["x"] - mover["pos"]["x"]) / gap
    dy = (target["pos"]["y"] - mover["pos"]["y"]) / gap
    mover["pos"] = {
        "x": max(0.0, min(BOARD_W, mover["pos"]["x"] + dx * step)),
        "y": max(0.0, min(BOARD_H, mover["pos"]["y"] + dy * step)),
    }
    new_gap = distance(mover["pos"], target["pos"])
    summary = (
        f"{mover['name']} 向 {target['name']} 移动 {step:.0f} 尺，现距 {new_gap:.0f} 尺"
        f"（本回合剩余移动力 {max(0.0, remaining - step):.0f} 尺）"
    )
    return mover, summary


def remove_combatant(room, cid):
    """
    把指定战斗者移出先攻条并校正回合指针（用于 GM 接任时放下自己的角色）。
    返回是否真的移除了。
    """
    combat = room.combat
    combatants = combat.get("combatants") or []
    current = combatants[combat.get("turn", 0)] if combatants else None
    survivors = [c for c in combatants if c.get("id") != cid]
    if len(survivors) == len(combatants):
        return False
    combat["combatants"] = survivors
    if survivors:
        ids = [c.get("id") for c in survivors]
        if current is not None and current.get("id") in ids:
            combat["turn"] = ids.index(current["id"])
        else:
            combat["turn"] = min(combat.get("turn", 0), len(survivors) - 1)
    else:
        combat["turn"] = 0
    return True


def cleanup_defeated(room):
    """
    把 HP 归零的怪物移出先攻条，并校正回合指针（当前行动者被移除时指针落在原位置的下一位）。
    返回被移出者的名字列表。
    """
    combat = room.combat
    combatants = combat.get("combatants") or []
    current = combatants[combat.get("turn", 0)] if combatants else None
    survivors = [
        c for c in combatants
        if not (c.get("kind") == "monster" and c.get("hp", 0) <= 0)
    ]
    if len(survivors) == len(combatants):
        return []
    survivor_ids = {c.get("id") for c in survivors}
    removed = [c.get("name", "") for c in combatants if c.get("id") not in survivor_ids]
    combat["combatants"] = survivors
    if survivors:
        ids = [c.get("id") for c in survivors]
        if current is not None and current.get("id") in ids:
            combat["turn"] = ids.index(current["id"])
        else:
            combat["turn"] = min(combat.get("turn", 0), len(survivors) - 1)
    else:
        combat["turn"] = 0
    return removed


def prune_combat(room):
    """
    战斗清理：移除倒地怪物；场上再无怪物时自动结束战斗（清空先攻条）。
    返回播报文本列表（由调用方写入日志/工具返回）。
    """
    combat = room.combat
    notes = []
    if not combat.get("active"):
        return notes
    removed = cleanup_defeated(room)
    if removed:
        notes.append("、".join(removed) + " 倒地，移出先攻条")
    if not any(c.get("kind") == "monster" for c in combat.get("combatants", [])):
        combat["active"] = False
        combat["combatants"] = []
        combat["round"] = 0
        combat["turn"] = 0
        notes.append("敌人全灭，战斗结束")
    return notes


def end_combat(room, reason=""):
    """主动结束战斗（玩家脱离/谈判成功/换场景）。返回播报文本。"""
    combat = room.combat
    if not combat.get("active"):
        return "当前不在战斗中"
    combat["active"] = False
    combat["combatants"] = []
    combat["round"] = 0
    combat["turn"] = 0
    text = "战斗结束"
    if reason:
        text += f"（{reason}）"
    return text


def spawn_monsters(room, specs):
    """
    生成怪物进先攻条（specs = [(怪物数据, 数量), ...]）。

    命名规则：同类型只有一只时用原名；同类型多只（本次或与场上已有合计）
    自动编号"地精1/地精2"，后续批次继续编号，早先未编号的会回溯补号。
    站位集中在板面右侧（x≈105 起），每 4 只换一行。
    战斗未开始时会自动开战（归档已上桌角色）。
    """
    combat = room.combat
    living_monsters = [
        c for c in combat.get("combatants", [])
        if c.get("kind") == "monster" and c.get("hp", 0) > 0
    ]
    if not combat.get("active") or not combat.get("combatants") or not living_monsters:
        # 无战斗、球场已空、或上一场敌人已全灭 → 视为新战斗：重置站位与先攻
        start_combat(room)
        combat = room.combat
    combatants = list(combat.get("combatants") or [])
    # 展开成每个个体一条，先算好每类的"需不需要编号"与编号起点
    pending = []
    for monster, count in specs:
        for _ in range(count):
            pending.append(monster)

    families = {}
    for monster in pending:
        families[monster["name"]] = families.get(monster["name"], 0) + 1
    needs_number = {}
    next_index = {}
    for base in families:
        existing = [c for c in combatants if _same_family(c.get("name", ""), base)]
        needs_number[base] = len(existing) + families[base] > 1
        if needs_number[base]:
            # 场上已有的同类回溯编号（地精 → 地精1），新一批从其后继续
            for index, combatant in enumerate(existing, start=1):
                combatant["name"] = f"{base}{index}"
            next_index[base] = len(existing)
        else:
            next_index[base] = 0

    added = []
    monster_index = sum(1 for c in combatants if c.get("kind") == "monster")
    for monster in pending:
        base = monster["name"]
        if needs_number[base]:
            next_index[base] += 1
            display_name = f"{base}{next_index[base]}"
        else:
            display_name = base
        init_roll = engine.roll_expr(f"1d20{monster.get('init_bonus', 0):+d}")
        pos = {
            "x": max(20.0, 105.0 - (monster_index // 4) * 12.0),
            "y": max(6.0, min(54.0, 18.0 + (monster_index % 4) * 8.0)),
        }
        monster_index += 1
        entry = {
            "id": room.next_id("m"),
            "name": display_name,
            "kind": "monster",
            "cr": monster["cr"],
            "cr_label": monster.get("cr_label", ""),
            "init": init_roll["total"],
            "hp": monster["hp"],
            "hp_max": monster["hp"],
            "ac": monster["ac"],
            "speed": monster.get("speed", 30),
            "pos": pos,
            "turn_start": dict(pos),
            "dashing": False,
        }
        combatants.append(entry)
        added.append(entry)
    combatants.sort(key=lambda item: item["init"], reverse=True)
    combat["combatants"] = combatants
    return added


def add_pc_to_combat(room, char):
    """
    角色在战斗进行中入场（选角/建卡/导入时调用）：
    掷先攻、放左侧默认站位、插入先攻序；已在先攻条里则原样返回。同伴 kind=ally。
    """
    combat = room.combat
    if not combat.get("active"):
        return None
    for combatant in combat.get("combatants", []):
        if combatant.get("id") == char.get("id"):
            return combatant
    init_roll = engine.roll_expr(f"1d20{char['mods']['dex']:+d}")
    init_total = characters.initiative_total(char, init_roll["total"])   # 「警觉」天赋：d20 下限 10
    pos = {"x": 15.0, "y": 30.0}
    entry = {
        "id": char["id"],
        "name": char["name"],
        "kind": "ally" if char.get("companion") else "pc",
        "init": init_total,
        "hp": char["hp"]["cur"],
        "hp_max": char["hp"]["max"],
        "speed": char.get("speed", 30),
        "pos": pos,
        "turn_start": dict(pos),
        "dashing": False,
    }
    combatants = list(combat.get("combatants") or [])
    combatants.append(entry)
    combatants.sort(key=lambda item: item["init"], reverse=True)
    combat["combatants"] = combatants
    return entry


def create_companion(room, name, klass_id, race_id="human", level=1, note=""):
    """
    招募一名 NPC 同伴：引擎按建卡规则自动构建完整角色卡（全推荐选择），
    按目标等级补足升级，标记 companion=True 后入库；战斗中自动进入先攻条。
    返回角色卡；非法职业/种族抛中文 ValueError。
    """
    name = str(name or "").strip()[:30]
    if not name:
        raise ValueError("同伴需要名字")
    payload = characters.build_companion_payload(str(klass_id), str(race_id or "human"))
    char = characters.compute_character(payload)
    char["name"] = name
    char["companion"] = True
    char["note"] = str(note or "")[:200]
    try:
        target_level = max(1, min(spells_mod.MAX_LEVEL, int(level or 1)))
    except (TypeError, ValueError):
        target_level = 1
    for _ in range(target_level - 1):
        spells_mod.level_up(char)
    characters.settle_companion_asi(char)          # 同伴的 ASI 自动按主属性结算
    characters.settle_companion_progression(char)  # 子职业/恩赐自动取第一项
    room.add_character(char)
    add_pc_to_combat(room, char)
    return char


def remove_companion(room, character_id):
    """移除同伴：从角色表、先攻条与磁盘上一起清掉（只允许移除 companion 标记的角色）。"""
    char = room.characters.get(str(character_id))
    if not char or not char.get("companion"):
        raise ValueError("同伴不存在")
    room.characters.pop(char["id"], None)
    room.combat["combatants"] = [
        c for c in room.combat.get("combatants", []) if c.get("id") != char["id"]
    ]
    try:
        (CHAR_DIR / f"{char['id']}.json").unlink(missing_ok=True)
    except OSError:
        pass
    return char


def remove_character(room, character_id):
    """
    删除角色卡（不可恢复）：从角色表、会话绑定、先攻条与磁盘文件一并移除。
    返回被删角色 dict；不存在抛 ValueError。调用方负责权限校验与播报。
    """
    cid = str(character_id or "")
    char = room.characters.pop(cid, None)
    if not char:
        raise ValueError("角色不存在")
    # 释放所有指向该角色的会话绑定（含 GM 卸任后的残留）
    for participant in room.participants.values():
        if participant.get("char_id") == cid:
            participant["char_id"] = None
    # 先攻条：移除并校正回合指针（删的是当前行动者时指针不跳过下一位）
    combat = room.combat
    combatants = combat.get("combatants") or []
    for idx, combatant in enumerate(combatants):
        if combatant.get("id") != cid:
            continue
        combatants.pop(idx)
        turn = int(combat.get("turn", 0))
        if turn > idx:
            combat["turn"] = turn - 1
        if combatants:
            combat["turn"] = min(int(combat.get("turn", 0)), len(combatants) - 1)
        else:
            combat.update({"active": False, "round": 0, "turn": 0})
        break
    try:
        (CHAR_DIR / f"{cid}.json").unlink(missing_ok=True)
    except OSError:
        pass
    return char


# ---------------------------------------------------------------------------
# 游戏时钟与休息（法术时长精细化的底层）
# ---------------------------------------------------------------------------


def advance_clock(room, minutes):
    """
    推进游戏时钟（旅行/搜索/休整等叙事时间），并按时钟结算所有角色的效果到期。
    返回播报文本列表（到期效果等）。单次推进上限 14 天（与 AI 工具层校验一致）。
    """
    try:
        minutes = max(0.0, float(minutes))
    except (TypeError, ValueError):
        minutes = 0.0
    minutes = min(minutes, 60.0 * 24 * 14)
    room.clock_minutes = round(room.clock_minutes + minutes, 2)
    notes = []
    for char in room.characters.values():
        expired = spells_mod.expire_by_clock(char, room.clock_minutes)
        if expired:
            room.persist_character(char)
            notes.extend(expired)
        # 稳定化恢复（BFRD）：稳定者 1d4 小时后自动回复 1 HP 并结束昏迷
        if (
            char.get("stable")
            and not char.get("dead")
            and char.get("stable_until") is not None
            and room.clock_minutes >= float(char["stable_until"])
        ):
            char["hp"]["cur"] = max(1, int(char["hp"]["cur"]))
            characters.stop_dying(char)
            room.persist_character(char)
            notes.append(f"{char.get('name', '')} 从稳定中苏醒：回复 1 点生命值（{char['hp']['cur']}/{char['hp']['max']}），倒地状态仍需起身")
    room.persist_room_meta()
    return notes


def short_rest(room):
    """
    短休（1 小时）：推进时钟并结算效果到期。
    （BFRD 无契约位；法师「奥术恢复」等短休恢复由玩家在角色卡信任制手动处理。）
    返回播报文本列表。
    """
    return advance_clock(room, 60)


def long_rest(room):
    """
    长休（8 小时）：推进时钟并结算效果到期；所有在场角色（含同伴）满血、
    法术位全恢复、全部效果与专注清空；力竭降低一级（BFRD：需进食饮水，平台视为满足）。
    休息开始时 HP < 1 的角色不获得好处（BFRD）；已死亡角色不参与。
    返回播报文本列表。
    """
    notes = advance_clock(room, 480)
    for char in room.characters.values():
        if not (char.get("owner_sid") or char.get("companion")):
            continue
        if char.get("dead"):
            continue
        if int(char["hp"]["cur"]) < 1:
            notes.append(f"{char['name']} 在长休开始时没有至少 1 点生命值，未能从长休中获益（需要治疗或稳定后苏醒）")
            continue
        healed = int(char["hp"]["max"]) - int(char["hp"]["cur"])
        char["hp"]["cur"] = char["hp"]["max"]
        for slot in (char.get("spell_slots") or {}).values():
            slot["used"] = 0
        # 生命骰恢复：上限的一半（至少 1 颗），BFRD 长休
        level = int(char.get("level") or 1)
        used = int(char.get("hit_dice_used") or 0)
        recover = min(used, max(1, level // 2))
        if recover:
            char["hit_dice_used"] = used - recover
        cleared = len(char.get("effects") or [])
        char["effects"] = []
        char["concentration"] = None
        details = []
        if healed:
            details.append(f"恢复 {healed} HP")
        if recover:
            details.append(f"恢复生命骰 ×{recover}")
        if cleared:
            details.append(f"清除 {cleared} 个效果")
        exhaustion = int(char.get("exhaustion") or 0)
        if exhaustion > 0:
            characters.apply_exhaustion(char, exhaustion - 1)
            details.append(f"力竭降至 {exhaustion - 1} 级")
        room.persist_character(char)
        # 同步战术板上的镜像 HP
        for combatant in room.combat.get("combatants", []):
            if combatant.get("id") == char["id"]:
                combatant["hp"] = char["hp"]["cur"]
                break
        notes.append(f"{char['name']} 完成长休" + ("（" + "，".join(details) + "）" if details else ""))
    return notes
