"""
FastAPI 路由与实时通信层：浏览器一切动作的入口与出口。

文件结构：
  1. 应用装配与全局单例（room/settings/hub/ai_lock/AI_STATE）
  2. Hub —— WebSocket 客户端注册与广播
  3. 权限与快照辅助（is_host / participant_or_none / can_dm / snapshot_message）
  4. ai_reply_task —— AI 回复的后台任务（含"思考中"状态广播与结果落日志）
  5. handle_message —— WebSocket 消息分发（玩家所有实时操作都走这里）
  6. HTTP 路由 —— 欢迎页/建卡/跑团页/管理页/存档/导出导入
  7. WebSocket 端点 /ws —— 连接生命周期与消息循环

约定：路由层不写业务规则——数值与规则校验一律调用 engine/characters/state；
本层只负责：解析请求、鉴权、调用引擎、广播结果。
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import actions as actions_mod, ai, campaign as campaign_mod, characters, debuglog, engine, netinfo, srd
from . import spells as spells_mod
from . import state as state_mod

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset_version():
    """静态资源版本号：取 static/ 下最新修改时间（秒），用于模板 URL 的缓存清除。"""
    try:
        latest = max(path.stat().st_mtime for path in (BASE_DIR / "static").glob("*") if path.is_file())
        return str(int(latest))
    except (ValueError, OSError):
        return "0"


TEMPLATES.env.globals["asset_version"] = _asset_version()

app = FastAPI(title="Weavlum")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# 局域网 IP 列表：启动时检测一次，欢迎页"分享给玩家"使用（网络变化时重启即可）
LAN_IPS = netinfo.candidate_ips()

# 玩家头像：文件存 data/avatars/，按昵称散列命名（重传即覆盖）；限制大小与格式
AVATARS_DIR = state_mod.DATA_DIR / "avatars"
AVATAR_MAX_BYTES = 512 * 1024
AVATAR_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
AVATAR_NAME_RE = re.compile(r"^[a-f0-9]{16}\.(png|jpg|webp|gif)$")

# --- 全局单例（单房间应用；重启后战役状态清空，角色卡从磁盘恢复）---
HOST_TOKEN = os.environ.get("WEAVLUM_HOST_TOKEN", "DEV")   # 由 run.py 注入并持久化
# AGPL §13：源码仓库地址（默认指向本项目仓库；部署者可用环境变量覆盖）
SOURCE_URL = os.environ.get(
    "WEAVLUM_SOURCE_URL", "https://github.com/Apolynoid/Weavlum"
).strip()
room = state_mod.RoomState()
settings = state_mod.load_settings()
ai_lock = asyncio.Lock()               # 串行化 AI 调用，避免多次回复交错
AI_STATE = {"pending": 0}              # 正在生成回复的任务数（前端"AI 思考中"指示）

# 调试模式：run.py --debug 时从进程启动第一秒就开始记录
if os.environ.get("WEAVLUM_DEBUG") == "1":
    debuglog.enable()


class Hub:
    """WebSocket 连接池：sid -> websocket。广播失败即摘除该连接。"""

    def __init__(self):
        self.clients = {}

    async def broadcast(self, payload):
        """给所有在线客户端发送同一份 JSON。"""
        # 调试日志：snapshot 记摘要（体积大），其余消息记全文
        if debuglog.is_enabled():
            ptype = payload.get("type")
            if ptype == "snapshot":
                state = payload.get("state") or {}
                combat = state.get("combat") or {}
                debuglog.log(
                    "ws",
                    "broadcast",
                    type=ptype,
                    receivers=list(self.clients),
                    summary={
                        "combat_active": combat.get("active"),
                        "turn": combat.get("turn"),
                        "round": combat.get("round"),
                        "chars": len(state.get("characters") or {}),
                        "log_len": len(state.get("log") or []),
                        "ai_mode": state.get("ai_mode"),
                        "ai_thinking": state.get("ai_thinking"),
                        "debug": state.get("debug"),
                    },
                )
            else:
                debuglog.log("ws", "broadcast", type=ptype, receivers=list(self.clients), payload=payload)
        data = json.dumps(payload, ensure_ascii=False)
        for sid in list(self.clients):
            ws = self.clients.get(sid)
            if ws is None:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.pop(sid, None)
        # 快照广播后顺带检查是否需要给 GM 推送「GM 秘闻」（含剧透）
        if payload.get("type") == "snapshot":
            await push_dm_brief()

    async def send(self, sid, payload):
        """只发给某个客户端（用于错误提示等私有消息）。"""
        debuglog.log("ws", "send", sid=sid, payload=payload)
        ws = self.clients.get(sid)
        if ws is None:
            return
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            self.clients.pop(sid, None)


hub = Hub()

# GM 秘闻推送缓存：内容或 GM 变更时才重发（避免每条快照都推大段剧透文本）
_dm_brief_cache = {"text": "", "sid": None}


async def push_dm_brief():
    """把含剧透的完整战役结构私发给当前 GM（GM 与 PC 界面差异的数据面）。"""
    dm_sid = room.dm_sid
    if not dm_sid or dm_sid not in hub.clients:
        return
    text = campaign_mod.render_dm_brief(room.campaign) if room.campaign else ""
    if text == _dm_brief_cache["text"] and dm_sid == _dm_brief_cache["sid"]:
        return
    _dm_brief_cache["text"] = text
    _dm_brief_cache["sid"] = dm_sid
    await hub.send(dm_sid, {"type": "dm_brief", "text": text})


# ---------------------------------------------------------------------------
# 权限与快照辅助
# ---------------------------------------------------------------------------


def is_host(request):
    """主机身份：cookie host == 启动时生成的 token（带 token 的链接访问一次即获得）。"""
    return request.cookies.get("host") == HOST_TOKEN


def participant_or_none(request):
    """从 cookie 取会话 sid；不在参与者名单里视为无效会话。"""
    sid = request.cookies.get("sid")
    if sid and sid in room.participants:
        return sid
    return None


def snapshot_message():
    """构造 snapshot 广播消息，附带 AI 思考状态与调试开关（供前端指示器/上报）。"""
    state = room.snapshot()
    state["ai_thinking"] = AI_STATE["pending"] > 0
    state["debug"] = debuglog.is_enabled()
    return {"type": "snapshot", "state": state}


def can_dm(sid):
    """
    GM 权限：只属于当前接任 GM 席位的玩家（自助接任、先到先得；主机不自动拥有）。
    """
    return bool(sid) and room.dm_sid == sid


def is_current_turn(cid):
    """判断某个战斗者是否是先攻条上的当前行动者。"""
    combatants = room.combat.get("combatants") or []
    turn = room.combat.get("turn", 0)
    return bool(combatants) and 0 <= turn < len(combatants) and combatants[turn].get("id") == cid


def campaign_summary():
    """管理页显示的一行战役结构摘要（未设置结构时返回空串）。"""
    if not room.campaign:
        return ""
    progress = campaign_mod.quest_progress(room.campaign)
    pressure = room.campaign.get("pressure") or {}
    completed = sum(tiers["completed"] for tiers in progress["tiers"].values())
    total = sum(tiers["total"] for tiers in progress["tiers"].values())
    return (
        f"{room.campaign.get('title', '未命名')} · 压力 {pressure.get('level', 0)}/{pressure.get('max', 6)}"
        f" · 任务完成 {completed}/{total}"
        + (" · 终局已开启" if progress["finale_unlocked"] else "")
    )


# ---------------------------------------------------------------------------
# AI 回复后台任务
# ---------------------------------------------------------------------------


async def ai_reply_task():
    """
    一次 AI 回复的完整生命周期（后台任务，不阻塞 WS 循环）：
      1. 挂"思考中"计数并广播（前端显示指示器）
      2. 加锁调用 ai.generate_reply（模型可能多轮调用工具）
      3. 广播快照（工具可能改变了战术板）、写事件日志、写掷骰日志、写叙述
      4. 触发滚动摘要更新（失败静默）
      5. 无论成败都减计数并广播状态
    """
    AI_STATE["pending"] += 1
    await hub.broadcast({"type": "ai_state", "thinking": True})
    try:
        async with ai_lock:
            try:
                text, rolls, notes = await ai.generate_reply(settings, room)
                # 工具可能改过战斗状态，先推一份快照给所有人
                await hub.broadcast(snapshot_message())
                for note in notes:
                    note_entry = room.add_log(
                        note.get("kind", "system"), note.get("who", "系统"), note.get("text", "")
                    )
                    await hub.broadcast({"type": "log", "entry": note_entry})
                for roll in rolls:
                    dc = roll.get("dc")
                    dc_txt = ""
                    if dc is not None:
                        dc_txt = f"（DC {dc}：{'成功' if roll.get('success') else '失败'}）"
                    line = f"AI GM 掷 {roll.get('reason', '骰子')}：{roll['breakdown']} = {roll['total']}{dc_txt}"
                    entry = room.add_log("roll", "AI GM", line, meta=roll)
                    await hub.broadcast({"type": "log", "entry": entry})
                if text:
                    entry = room.add_log("narrate", "AI GM", text)
                    await hub.broadcast({"type": "log", "entry": entry})
                else:
                    # 模型没有给出正文（常见于思考型模型输出预算被推理占满）
                    entry = room.add_log(
                        "system",
                        "系统",
                        "AI 没有输出正文（思考模型可能占满了输出预算），请再试一次，或在主机管理页更换模型。",
                    )
                    await hub.broadcast({"type": "log", "entry": entry})
                try:
                    await ai.maybe_update_summary(settings, room)
                except Exception:
                    pass
            except Exception as exc:
                debuglog.log("ai", "error", error=str(exc))
                entry = room.add_log("system", "系统", f"AI 调用失败：{exc}")
                await hub.broadcast({"type": "log", "entry": entry})
    finally:
        AI_STATE["pending"] -= 1
        await hub.broadcast({"type": "ai_state", "thinking": AI_STATE["pending"] > 0})


# ---------------------------------------------------------------------------
# WebSocket 消息分发：玩家端的全部实时操作
# ---------------------------------------------------------------------------


# 背包类 WS 消息 → 动作层动作名（玩家自助与 GM 代改统一走 actions.perform）
_INV_ACTION_MAP = {
    "inv_add": "add_item",
    "inv_remove": "remove_item",
    "inv_buy": "buy_item",
    "inv_note": "set_item_note",
    "inv_sell": "sell_item",
    "inv_coins": "set_coins",
    "inv_exchange": "exchange_currency",
    "choose_asi": "apply_asi",
    "choose_subclass": "choose_subclass",
    "choose_boon": "choose_boon",
    "luck_gain": "grant_luck",
    "luck_spend": "spend_luck",
    "spell_learn": "learn_spell",
    "spell_forget": "forget_spell",
    "spell_swap": "swap_spell",
    "hit_die_spend": "spend_hit_dice",
    "wear_armor": "wear_armor",
}


async def handle_inventory_message(sid, message):
    """
    背包操作（角色卡 UI 自助 / GM 代改）：
    仅限本人角色或 GM；统一委托动作层（系统第一公民），成功后广播快照并写共享日志。
    """
    cid = str(message.get("character_id", ""))
    char = room.characters.get(cid)
    if not char:
        return
    if not (can_dm(sid) or char.get("owner_sid") == sid):
        return
    kind = message.get("type")
    if kind == "inv_coins":
        args = {"character": cid, "coins": message.get("coins") or {}}
    elif kind == "inv_exchange":
        args = {
            "character": cid,
            "from": message.get("from", ""),
            "to": message.get("to", ""),
            "amount": message.get("amount", 1),
        }
    elif kind == "choose_asi":
        args = {"character": cid, "asi": message.get("asi") or {}, "talent": message.get("talent") or ""}
    elif kind == "choose_subclass":
        args = {"character": cid, "subclass": message.get("subclass", "")}
    elif kind == "choose_boon":
        args = {"character": cid, "level": message.get("level"), "boon": message.get("boon", "")}
    elif kind == "luck_gain":
        args = {"character": cid, "amount": message.get("amount", 1), "reason": message.get("reason", "")}
    elif kind == "luck_spend":
        args = {"character": cid, "amount": message.get("amount", 1), "reason": message.get("reason", "")}
    elif kind == "spell_learn":
        args = {"character": cid, "spell": message.get("spell", ""),
                "bucket": message.get("bucket") or None, "free": message.get("free", True)}
    elif kind == "spell_forget":
        args = {"character": cid, "spell": message.get("spell", ""), "bucket": message.get("bucket") or None}
    elif kind == "spell_swap":
        args = {"character": cid, "old": message.get("old", ""), "spell": message.get("spell", "")}
    elif kind == "hit_die_spend":
        args = {"character": cid, "count": message.get("count", 1)}
    elif kind == "wear_armor":
        args = {"character": cid, "armor": message.get("armor"), "shield": message.get("shield")}
    elif kind == "inv_note":
        args = {
            "character": cid,
            "item": message.get("item", ""),
            "note": message.get("note", ""),
            "old_note": message.get("old_note", ""),
        }
    else:
        args = {"character": cid, "item": message.get("item", ""), "qty": message.get("qty", 1),
                "note": message.get("note", "")}
    try:
        outcome = actions_mod.perform(room, _INV_ACTION_MAP.get(kind, ""), args)
    except (ValueError, TypeError) as exc:
        await hub.send(sid, {"type": "error", "error": str(exc)})
        return
    for text in outcome.get("notes", []):
        entry = room.add_log("system", "系统", text)
        await hub.broadcast({"type": "log", "entry": entry})
    if outcome.get("changed"):
        await hub.broadcast(snapshot_message())


async def handle_message(sid, message):
    """
    处理一条来自浏览器的 WS 消息。结构为 {type: ..., ...}。

    权限模型简述：
      - 掷骰/检定/移动自己的棋子：任何在场玩家
      - 战斗控制（开战/回合/怪物 HP/加怪）与 GM 叙述：can_dm（当前 GM）
      - 主持模式总闸：仅主机（服务器管理员），在管理页或桌面切换
      - GM 席位：玩家自助接任/卸任（claim_dm / release_dm），先到先得
      - 自己的角色卡 HP：本人或 GM
    """
    p = room.participants.get(sid)
    if not p:
        return
    debuglog.log("ws", "in", sid=sid, message=message)
    kind = message.get("type")
    if kind == "chat":
        # 玩家发言：写日志广播；AI 模式下（或 @ai /ai 前缀）触发 GM 回复
        text = str(message.get("text", "")).strip()[:2000]
        if not text:
            return
        entry = room.add_log("chat", p["nickname"], text)
        await hub.broadcast({"type": "log", "entry": entry})
        # AI 自动接管仅限"无 GM 席位且总闸开着"；有人接任 GM 后由人类主持，@ai 前缀仍可唤起助手
        if (room.ai_mode and not room.dm_sid) or text.startswith(("@ai", "/ai")):
            asyncio.create_task(ai_reply_task())
    elif kind == "roll":
        # 自由掷骰表达式（引擎计算，错误只回给发起者）
        expr = str(message.get("expr", "")).strip()
        reason = str(message.get("reason", "")).strip()[:60]
        try:
            result = engine.roll_expr(expr)
        except ValueError as exc:
            await hub.send(sid, {"type": "error", "error": str(exc)})
            return
        line = f"{p['nickname']} 掷 {reason or '骰子'}：{result['breakdown']} = {result['total']}"
        entry = room.add_log("roll", p["nickname"], line, meta=result)
        await hub.broadcast({"type": "log", "entry": entry})
    elif kind == "check":
        # 面板检定：客户端提交属性加值/DC/优劣势，引擎掷骰并判定
        try:
            modifier = int(message.get("modifier") or 0)
        except (TypeError, ValueError):
            modifier = 0
        dc = message.get("dc")
        try:
            dc = int(dc) if dc not in (None, "") else None
        except (TypeError, ValueError):
            dc = None
        mode = str(message.get("mode", "normal"))
        reason = str(message.get("reason", "")).strip()[:60]
        result = engine.roll_check(modifier=modifier, dc=dc, mode=mode, label=reason)
        dc_txt = ""
        if dc is not None:
            dc_txt = f"（DC {dc}：{'成功' if result['success'] else '失败'}）"
        line = f"{p['nickname']} 掷 {reason or '检定'}：{result['breakdown']}{dc_txt}"
        entry = room.add_log("roll", p["nickname"], line, meta=result)
        await hub.broadcast({"type": "log", "entry": entry})
    elif kind == "set_hp":
        # 角色卡 HP 直接设置（信任制）：本人或 GM 可改，留日志痕迹由引擎侧无感
        char = room.characters.get(str(message.get("character_id", "")))
        if not char:
            return
        if not (can_dm(sid) or char.get("owner_sid") == sid):
            return
        try:
            value = int(message.get("value"))
        except (TypeError, ValueError):
            return
        char["hp"]["cur"] = max(0, min(char["hp"]["max"], value))
        room.persist_character(char)
        await hub.broadcast(snapshot_message())
    elif kind == "start_combat":
        if can_dm(sid):
            state_mod.start_combat(room)
            await hub.broadcast(snapshot_message())
    elif kind == "next_turn":
        if can_dm(sid):
            expired = state_mod.next_turn(room)
            for text in expired:
                entry = room.add_log("system", "系统", text)
                await hub.broadcast({"type": "log", "entry": entry})
            await hub.broadcast(snapshot_message())
    elif kind == "set_slot":
        # 法术位手动调整（信任制）：本人或 GM 可改
        char = room.characters.get(str(message.get("character_id", "")))
        if not char:
            return
        if not (can_dm(sid) or char.get("owner_sid") == sid):
            return
        slot = (char.get("spell_slots") or {}).get(str(message.get("level", "")))
        if not slot:
            return
        try:
            used = int(message.get("used"))
        except (TypeError, ValueError):
            return
        slot["used"] = max(0, min(slot.get("max", 0), used))
        room.persist_character(char)
        await hub.broadcast(snapshot_message())
    elif kind == "rest":
        # 手动短休/长休（GM/主机；AI 模式下由 AI 走 short_rest/long_rest 工具）
        if not can_dm(sid):
            return
        rest_kind = str(message.get("kind", "short"))
        notes = state_mod.long_rest(room) if rest_kind == "long" else state_mod.short_rest(room)
        for text in notes:
            entry = room.add_log("system", "系统", text)
            await hub.broadcast({"type": "log", "entry": entry})
        await hub.broadcast(snapshot_message())
    elif kind == "drop_effect":
        # 手动解除效果/专注（本人或 GM）
        char = room.characters.get(str(message.get("character_id", "")))
        if not char:
            return
        if not (can_dm(sid) or char.get("owner_sid") == sid):
            return
        try:
            text = spells_mod.remove_effect(char, message.get("name", ""))
        except ValueError:
            return
        room.persist_character(char)
        entry = room.add_log("system", "系统", text)
        await hub.broadcast({"type": "log", "entry": entry})
        await hub.broadcast(snapshot_message())
    elif kind == "set_monster_hp":
        # 先攻条上怪物 HP 调整（GM）：delta=任意增减；value=设为精确值；打到 0 自动清理与判负
        if not can_dm(sid):
            return
        try:
            if message.get("value") is not None:
                delta, value = None, int(message.get("value"))
            else:
                delta, value = int(message.get("delta") or 0), None
        except (TypeError, ValueError):
            return
        found, pruned = state_mod.set_monster_hp(room, str(message.get("id", "")), delta=delta, value=value)
        if not found:
            return
        for text in pruned:
            entry = room.add_log("system", "系统", text)
            await hub.broadcast({"type": "log", "entry": entry})
        await hub.broadcast(snapshot_message())
    elif kind == "end_combat":
        # 手动结束战斗（GM/主机；AI 模式下由 AI 的 end_combat 工具处理）
        if not can_dm(sid):
            return
        text = state_mod.end_combat(room, str(message.get("reason", "") or "手动结束"))
        entry = room.add_log("system", "系统", text)
        await hub.broadcast({"type": "log", "entry": entry})
        await hub.broadcast(snapshot_message())
    elif kind == "move_token":
        # 手动拖拽棋子：本人（PC）或 GM；玩家仅限自己回合且受速度限制
        combatant = next(
            (c for c in room.combat.get("combatants", []) if c.get("id") == str(message.get("id", ""))),
            None,
        )
        if not combatant or not isinstance(combatant.get("pos"), dict):
            return
        try:
            x = float(message.get("x"))
            y = float(message.get("y"))
        except (TypeError, ValueError):
            return
        mine = combatant.get("kind") == "pc" and p.get("char_id") == combatant.get("id")
        if not mine and not can_dm(sid):
            return
        if mine and room.combat.get("active"):
            # 玩家移动自己的棋子：必须轮到自己，且不得超过剩余移动力
            if not is_current_turn(combatant["id"]):
                await hub.send(sid, {"type": "error", "error": "还没轮到你行动"})
                return
            start = combatant.get("turn_start") or combatant["pos"]
            dist = state_mod.distance(start, {"x": x, "y": y})
            allowed = float(combatant.get("speed", 30)) * (2 if combatant.get("dashing") else 1)
            if dist > allowed + 0.5:
                remaining = max(0.0, allowed - state_mod.distance(start, combatant["pos"]))
                await hub.send(sid, {"type": "error", "error": f"超出移动力：这回合只剩 {remaining:.0f} 尺"})
                return
        state_mod.move_token(room, combatant["id"], x, y)
        await hub.broadcast(snapshot_message())
    elif kind == "toggle_dash":
        # 冲刺开关：仅当前行动者本人可用；回合流转时会自动清空
        cid = p.get("char_id")
        combatant = next((c for c in room.combat.get("combatants", []) if c.get("id") == cid), None)
        if combatant and room.combat.get("active") and is_current_turn(cid):
            combatant["dashing"] = not combatant.get("dashing", False)
            await hub.broadcast(snapshot_message())
    elif kind == "add_combatant":
        # GM 手动加怪（不经过 AI 的备用通道；AI 模式一般由 AI 走 spawn_monsters）
        if not can_dm(sid):
            return
        name = str(message.get("name", "")).strip()[:30] or "怪物"
        try:
            hp = max(1, min(999, int(message.get("hp") or 10)))
        except (TypeError, ValueError):
            hp = 10
        try:
            bonus = int(message.get("init_bonus") or 0)
        except (TypeError, ValueError):
            bonus = 0
        roll = engine.roll_expr(f"1d20{bonus:+d}")
        combat = room.combat
        combatants = list(combat.get("combatants") or [])
        combatants.append(
            {
                "id": room.next_id("m"),
                "name": name,
                "kind": "monster",
                "init": roll["total"],
                "hp": hp,
                "hp_max": hp,
            }
        )
        combatants.sort(key=lambda item: item["init"], reverse=True)
        combat["combatants"] = combatants
        if not combat.get("active"):     # 没有战斗时手动加怪视为开一场新战斗
            combat["active"] = True
            combat["round"] = 1
            combat["turn"] = 0
        await hub.broadcast(snapshot_message())
    elif kind == "action":
        # 通用动作入口（人类 GM 控制台）：与 AI 工具共用 actions.perform
        # —— 系统/引擎是第一公民，AI 与人类 GM 是同等权重的调用方
        if not can_dm(sid):
            return
        try:
            outcome = actions_mod.perform(room, message.get("name", ""), message.get("args") or {})
        except (ValueError, TypeError) as exc:
            await hub.send(sid, {"type": "error", "error": str(exc)})
            return
        for text in outcome.get("notes", []):
            entry = room.add_log("system", "系统", text)
            await hub.broadcast({"type": "log", "entry": entry})
        if outcome.get("changed"):
            await hub.broadcast(snapshot_message())
        confirm = outcome.get("text") or ""
        if confirm:
            entry = {
                "id": f"ack-{int(time.time() * 1000)}",
                "ts": time.time(),
                "kind": "system",
                "who": "系统",
                "text": confirm,
            }
            await hub.send(sid, {"type": "log", "entry": entry})
    elif kind in ("inv_add", "inv_remove", "inv_buy", "inv_sell", "inv_coins", "inv_exchange",
                  "choose_asi", "choose_subclass", "choose_boon", "luck_gain", "luck_spend",
                  "spell_learn", "spell_forget", "spell_swap", "hit_die_spend", "wear_armor", "inv_note"):
        # 背包/属性提升操作（本人或 GM）：走动作层，留共享日志
        await handle_inventory_message(sid, message)
    elif kind == "dm_narrate":
        # 人类 GM 以"GM"名义发布叙述（与 AI 的 narrate 通道对等）
        if not can_dm(sid):
            return
        text = str(message.get("text", "")).strip()[:2000]
        if not text:
            return
        entry = room.add_log("narrate", "GM", text)
        await hub.broadcast({"type": "log", "entry": entry})
    elif kind == "set_mode":
        # 主持模式总闸（仅主机/服务器管理员）：切到 AI 主持会立即清空 GM 席位
        if not p.get("is_host"):
            return
        room.ai_mode = bool(message.get("ai_mode"))
        cleared = False
        if room.ai_mode and room.dm_sid:
            room.dm_sid = None
            cleared = True
        room.persist_room_meta()
        await hub.broadcast(snapshot_message())
        if cleared:
            entry = room.add_log("system", "系统", "管理员切回 AI 主持，GM 席位已清空。")
            await hub.broadcast({"type": "log", "entry": entry})
    elif kind == "claim_dm":
        # 玩家自助接任 GM 席位（先到先得；当 GM 即放下当前角色）
        ok, err = room.claim_dm(sid)
        if not ok:
            await hub.send(sid, {"type": "error", "error": err})
            return
        entry = room.add_log("system", "系统", f"{p['nickname']} 接过了 GM 席位，开始主持。")
        await hub.broadcast(snapshot_message())
        await hub.broadcast({"type": "log", "entry": entry})
    elif kind == "release_dm":
        # GM 主动卸任（仅本人）
        if room.dm_sid != sid:
            return
        room.release_dm(sid)
        entry = room.add_log("system", "系统", f"{p['nickname']} 卸下了 GM 席位。")
        await hub.broadcast(snapshot_message())
        await hub.broadcast({"type": "log", "entry": entry})
    elif kind == "kick_player":
        # GM 移出离线玩家（在线玩家由 WS 连接生命周期管理，不在此处理）
        if not can_dm(sid):
            await hub.send(sid, {"type": "error", "error": "只有 GM 能移出玩家"})
            return
        target_sid = str(message.get("sid") or "").strip()
        ok, err, entry = room.kick_offline_player(target_sid, sid)
        if not ok:
            await hub.send(sid, {"type": "error", "error": err})
            return
        await hub.broadcast(snapshot_message())
        await hub.broadcast({"type": "log", "entry": entry})


# ---------------------------------------------------------------------------
# HTTP 路由：页面
# ---------------------------------------------------------------------------


@app.get("/")
async def welcome(request: Request):
    """
    欢迎页：可带 ?host_token=XXX 获得主机身份（种 cookie）。
    列出已有角色（含"使用中"状态）供直接选角入场。
    """
    token = request.query_params.get("host_token")
    viewer_sid = request.cookies.get("sid")
    # 本次请求携带正确 token 也算主机（cookie 在响应末尾才种，首访即可见管理操作）
    host_viewer = is_host(request) or (token is not None and token == HOST_TOKEN)
    existing = []
    for cid, char in room.characters.items():
        owner = char.get("owner_sid")
        # 使用中 = 已分配给玩家（含离线）；离线玩家需由 GM 在游戏中移出后释放角色
        owner_online = bool(
            owner and owner in room.participants and room.participants[owner].get("connected")
        )
        owner_is_me = bool(owner and viewer_sid and owner == viewer_sid)
        existing.append(
            {
                "id": cid,
                "name": char["name"],
                "klass": char["klass"],
                "race": char["race"],
                "level": char["level"],
                "owner_nick": char.get("owner_nick"),
                "in_use": bool(owner),
                "owner_online": owner_online,
                "owner_is_me": owner_is_me,
                # 删除权限：主机 / GM 席位 / 角色本人（旧卡无主时仅主机或 GM 可删）
                "can_delete": bool(
                    host_viewer
                    or (viewer_sid and (viewer_sid == room.dm_sid or owner == viewer_sid))
                ),
            }
        )
    dm_sid = room.dm_sid
    context = {
        "existing": existing,
        "is_host": host_viewer,
        "share_urls": [f"http://{ip}:{request.url.port}/" for ip in LAN_IPS],
        "lan_detected": bool(LAN_IPS),
        "local_url": f"http://localhost:{request.url.port}/",
        # 调色板（欢迎页选颜色用）
        "player_colors": json.dumps(list(state_mod.PLAYER_COLORS)),
        # GM 席位信息：占用者昵称 + 是否就是当前浏览器（用于"继续以 GM 入场"）
        "dm_occupied": bool(dm_sid),
        "dm_name": ((room.participants.get(dm_sid) or {}).get("nickname") or "另一位玩家") if dm_sid else "",
        "dm_is_me": bool(dm_sid and request.cookies.get("sid") == dm_sid),
    }
    response = TEMPLATES.TemplateResponse(request, "welcome.html", context)
    if token is not None:
        # token 正确才写入 host cookie（错误的 token 会清掉旧身份）
        response.set_cookie("host", token if token == HOST_TOKEN else "", httponly=True, samesite="lax")
    return response


@app.get("/create")
async def create_page(request: Request):
    """建卡向导页：把规则数据/常量嵌入页面交给前端渲染。"""
    sid = participant_or_none(request)
    if not sid:
        return RedirectResponse("/")
    context = {
        "is_host": is_host(request),
        "nickname": room.participants[sid]["nickname"],
        "srd": json.dumps(srd.wizard_options(), ensure_ascii=False),
        "ability_names": json.dumps(characters.ABILITY_NAMES, ensure_ascii=False),
        "standard_array": json.dumps(characters.STANDARD_ARRAY),
        "talent_schools": json.dumps(characters.SPELL_SCHOOLS, ensure_ascii=False),
        "spells": json.dumps(srd.spells(), ensure_ascii=False),
        "spell_rules": json.dumps(srd.spell_rules(), ensure_ascii=False),
        # 物品库（悬停说明/自动补全用）与职业装备选项组（（a）/（b）选择器唯一数据源）
        "items": json.dumps(srd.items(), ensure_ascii=False),
        "equip_specs": json.dumps(
            {cid: characters.equipment_specs(cid) for cid in srd.classes()},
            ensure_ascii=False,
        ),
    }
    return TEMPLATES.TemplateResponse(request, "create.html", context)


@app.get("/play")
async def play_page(request: Request):
    """跑团桌页面。"""
    sid = participant_or_none(request)
    if not sid:
        return RedirectResponse("/")
    # 当前玩家的固定掷骰（未设置时为 None，由前端回退到默认按钮）
    profile = room.player_profiles.get(room.participants[sid]["nickname"]) or {}
    context = {
        "is_host": is_host(request),
        "sid": sid,
        "quick_rolls": profile.get("quick_rolls"),
        "ability_names": json.dumps(characters.ABILITY_NAMES, ensure_ascii=False),
        "skills": json.dumps(srd.skills(), ensure_ascii=False),
        # 怪物图鉴（25 只完整数据：名字/CR/攻防/描述）：GM 生成下拉与悬停图鉴共用
        "monsters": json.dumps(srd.monsters(), ensure_ascii=False),
        # 全部法术（169 条）：GM 控制台"学 / 忘法术"下拉用
        "spells": json.dumps(srd.spells(), ensure_ascii=False),
        # 全部种族（含特性全描述）：角色卡悬停说明用
        "races": json.dumps(srd.races(), ensure_ascii=False),
        # 物品库（370 条）：角色卡背包/悬停说明/交易下拉用
        "items": json.dumps(srd.items(), ensure_ascii=False),
        # 全部天赋（BFRD 30 条）：角色卡天赋行悬停说明用
        "talents": json.dumps(srd.talents(), ensure_ascii=False),
        # 职业元数据（子职业名 / 提升天赋类别 / 10·20 级恩赐选项）：角色卡待选区块用
        "class_meta": json.dumps({
            cid: {
                "name": klass.get("name", cid),
                "spell_ability": klass.get("spell_ability"),
                "sources": klass.get("sources") or [],
                "improvement_talents": klass.get("improvement_talents") or [],
                "subclasses": {sid: sub.get("name", sid) for sid, sub in (klass.get("subclasses") or {}).items()},
                "progression": klass.get("progression") or {},
                "boons": {
                    str(lv): [{"key": o["key"], "name": o["name"]} for o in characters.boon_options(cid, lv)]
                    for lv in (10, 20)
                },
            }
            for cid, klass in srd.classes().items()
        }, ensure_ascii=False),
        # 职业/子职业特性文本（角色卡"职业特性"区悬停说明用）
        "features": json.dumps(actions_mod.class_features_payload(), ensure_ascii=False),
        # 状态表 16 条（效果行悬停判定用）
        "conditions": json.dumps(srd.conditions(), ensure_ascii=False),
    }
    return TEMPLATES.TemplateResponse(request, "play.html", context)


@app.get("/credits")
async def credits_page(request: Request):
    """许可与致谢页（AGPL 源码入口 + ORC 声明 + SRD 5.1 CC-BY-4.0 署名；公开页）。"""
    return TEMPLATES.TemplateResponse(request, "credits.html", {"source_url": SOURCE_URL})


@app.get("/manage")
async def manage_page(request: Request):
    """
    服务器管理页（独立界面，与玩家页面互不链接）：
    - 带正确 host_token 访问：种下主机 cookie 并直接进入（启动终端打印的就是这个网址）
    - 已持有主机 cookie：直接进入；否则重定向回入口页
    """
    token = request.query_params.get("host_token")
    token_ok = token is not None and token == HOST_TOKEN
    if token is not None and not token_ok:
        return RedirectResponse("/")
    if not token_ok and not is_host(request):
        return RedirectResponse("/")
    context = {
        "is_host": True,
        "settings": settings,
        "saves": state_mod.list_saves(),
        "participants": room.snapshot()["participants"],
        # 总闸显示：有 GM 席位时一律显示"人类 GM"；席位空时按总闸值显示
        "ai_effective": room.ai_mode and not room.dm_sid,
        "dm_sid": room.dm_sid,
        "dm_name": ((room.participants.get(room.dm_sid) or {}).get("nickname") or "（离线玩家）") if room.dm_sid else "",
        "dm_online": bool(room.dm_sid and room.dm_sid in hub.clients),
        "world": room.world,
        "dm_note": room.dm_note,
        "summary_len": len(room.summary),
        "log_len": len(room.log),
        "debug_enabled": debuglog.is_enabled(),
        "debug_file": (Path(debuglog.current_file()).name if debuglog.current_file() else ""),
        "debug_files": debuglog.list_files(),
        "campaign_json": (json.dumps(room.campaign, ensure_ascii=False, indent=2) if room.campaign else ""),
        "campaign_summary": campaign_summary(),
        "companions": [
            {"id": cid, "name": c["name"], "race": c["race"], "klass": c["klass"], "level": c["level"]}
            for cid, c in room.characters.items()
            if c.get("companion")
        ],
        "class_options": sorted(
            [(cid, data["name"]) for cid, data in srd.classes().items()],
            key=lambda item: item[1],
        ),
    }
    response = TEMPLATES.TemplateResponse(request, "manage.html", context)
    if token_ok:
        response.set_cookie("host", HOST_TOKEN, httponly=True, samesite="lax")
    return response


# ---------------------------------------------------------------------------
# HTTP 路由：玩家动作 API
# ---------------------------------------------------------------------------


@app.post("/api/join")
async def api_join(request: Request):
    """
    入场第一步：登记昵称。复用已有 sid cookie（同一浏览器算同一玩家），
    否则新发一个。主机身份的 cookie 会被记录到参与者信息里。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    nickname = str(body.get("nickname", "")).strip()[:20]
    if not nickname:
        return JSONResponse({"ok": False, "error": "请输入昵称"}, status_code=400)
    sid = request.cookies.get("sid") or secrets.token_urlsafe(12)
    previous = room.participants.get(sid, {})
    # 玩家档案（颜色/头像按昵称持久化）：缺失颜色时自动分配
    profile = room.ensure_profile(
        nickname,
        color=str(body.get("color", "")).strip() or None,
        avatar=str(body.get("avatar", "")).strip() or None,
    )
    room.participants[sid] = {
        "nickname": nickname,
        "connected": sid in hub.clients,
        "char_id": previous.get("char_id"),
        "is_host": request.cookies.get("host") == HOST_TOKEN,
        "color": profile.get("color", ""),
        "avatar": profile.get("avatar", ""),
    }
    response = JSONResponse({"ok": True})
    response.set_cookie("sid", sid, httponly=True, samesite="lax")
    await hub.broadcast(snapshot_message())
    return response


@app.post("/api/claim_dm")
async def api_claim_dm(request: Request):
    """
    玩家自助接任 GM 席位（欢迎页入口）：先到先得；已有 GM 时拒绝（409 带提示）。
    成功返回 ok，由前端跳转 /play；已是 GM 本人时幂等成功（重连/刷新继续任）。
    """
    sid = participant_or_none(request)
    if not sid:
        return JSONResponse({"ok": False, "error": "请先输入昵称"}, status_code=400)
    ok, err = room.claim_dm(sid)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=409)
    entry = room.add_log("system", "系统", f"{room.participants[sid]['nickname']} 接过了 GM 席位，开始主持。")
    await hub.broadcast(snapshot_message())
    await hub.broadcast({"type": "log", "entry": entry})
    return JSONResponse({"ok": True})


@app.post("/api/avatar")
async def api_avatar(request: Request, file: UploadFile = File(...)):
    """
    上传头像：png/jpg/webp/gif，≤ 512KB；按昵称散列命名（重传覆盖旧图）。
    需要已登记昵称（先 /api/join）。返回文件名，前端入场时随 join 提交。
    """
    sid = participant_or_none(request)
    if not sid:
        return JSONResponse({"ok": False, "error": "请先输入昵称"}, status_code=400)
    ext = AVATAR_TYPES.get(file.content_type or "")
    if not ext:
        return JSONResponse({"ok": False, "error": "只支持 png / jpg / webp / gif 图片"}, status_code=400)
    content = await file.read()
    if not content:
        return JSONResponse({"ok": False, "error": "空文件"}, status_code=400)
    if len(content) > AVATAR_MAX_BYTES:
        return JSONResponse({"ok": False, "error": "图片不能超过 512KB"}, status_code=400)
    nickname = room.participants[sid]["nickname"]
    name = hashlib.sha1(nickname.encode("utf-8")).hexdigest()[:16] + "." + ext
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    (AVATARS_DIR / name).write_bytes(content)
    profile = room.ensure_profile(nickname, avatar=name)
    room.participants[sid]["avatar"] = name
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True, "avatar": name})


@app.get("/avatars/{name}")
async def avatar_file(name: str):
    """头像文件服务：只接受散列命名（防目录穿越），文件不存在时 404。"""
    if not AVATAR_NAME_RE.fullmatch(name):
        return JSONResponse({"ok": False, "error": "不存在"}, status_code=404)
    path = AVATARS_DIR / name
    if not path.exists():
        return JSONResponse({"ok": False, "error": "不存在"}, status_code=404)
    return FileResponse(path)


@app.post("/api/pick_character")
async def api_pick(request: Request):
    """
    选择一个已有角色入场：
      - 已被其他玩家选用的角色禁止抢占（在线/离线一致；离线由 GM 移出玩家后释放）
      - 换角色时释放旧角色的所有权
      - 战斗进行中会自动加入先攻条
    """
    sid = participant_or_none(request)
    if not sid:
        return JSONResponse({"ok": False, "error": "请先输入昵称"}, status_code=400)
    if sid == room.dm_sid:
        return JSONResponse({"ok": False, "error": "你正在担任 GM——先卸任才能选择角色"}, status_code=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    cid = str(body.get("character_id", ""))
    char = room.characters.get(cid)
    if not char:
        return JSONResponse({"ok": False, "error": "角色不存在"}, status_code=404)
    owner = char.get("owner_sid")
    if owner and owner != sid:
        if room.participants.get(owner, {}).get("connected"):
            return JSONResponse({"ok": False, "error": "该角色正在被其他在线玩家使用"}, status_code=409)
        return JSONResponse(
            {"ok": False, "error": "该角色已被其他玩家选用（当前离线）；请让 GM 在游戏中移出该玩家以释放角色"},
            status_code=409,
        )
    # 放弃此前使用的角色
    previous_cid = room.participants[sid].get("char_id")
    if previous_cid and previous_cid != cid and previous_cid in room.characters:
        room.characters[previous_cid]["owner_sid"] = None
        room.characters[previous_cid]["owner_nick"] = None
        room.persist_character(room.characters[previous_cid])
    char["owner_sid"] = sid
    char["owner_nick"] = room.participants[sid]["nickname"]
    room.persist_character(char)
    room.participants[sid]["char_id"] = cid
    state_mod.add_pc_to_combat(room, char)
    entry = room.add_log("system", "系统", f"{room.participants[sid]['nickname']} 以 {char['name']} 的身份加入了冒险。")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True})


@app.post("/api/quick_rolls")
async def api_quick_rolls(request: Request):
    """
    保存当前玩家的自定义固定掷骰（最多 8 条，只影响本人档案）：
      - label 最长 20 字，留空则用表达式作为按钮名
      - expr 交给 engine 校验（支持 1d20+5 / 2d6+3 等）
      - 允许保存空列表（清空所有固定掷骰）
    """
    sid = participant_or_none(request)
    if not sid:
        return JSONResponse({"ok": False, "error": "请先输入昵称"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    rows = body.get("rolls")
    if not isinstance(rows, list):
        return JSONResponse({"ok": False, "error": "格式错误"}, status_code=400)
    if len(rows) > 8:
        return JSONResponse({"ok": False, "error": "固定掷骰最多 8 条"}, status_code=400)
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        expr = str(row.get("expr") or "").strip()[:40]
        if not expr:
            continue
        label = str(row.get("label") or "").strip()[:20] or expr
        try:
            engine.roll_expr(expr)
        except ValueError as exc:
            return JSONResponse(
                {"ok": False, "error": f"「{label}」的表达式无效：{exc}"}, status_code=400
            )
        cleaned.append({"label": label, "expr": expr})
    saved = room.set_quick_rolls(room.participants[sid]["nickname"], cleaned)
    return JSONResponse({"ok": True, "rolls": saved})


@app.post("/api/create")
async def api_create(request: Request):
    """
    建卡提交：引擎计算（characters.compute_character）→ 入库落盘 →
    绑定给当前会话 → 战斗中加入先攻条 → 广播。
    校验错误原样返回给前端显示。
    """
    sid = participant_or_none(request)
    if not sid:
        return JSONResponse({"ok": False, "error": "会话已失效，请回到入口页"}, status_code=400)
    if sid == room.dm_sid:
        return JSONResponse({"ok": False, "error": "你正在担任 GM——先卸任才能创建角色"}, status_code=409)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求格式错误"}, status_code=400)
    try:
        char = characters.compute_character(body)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    char["owner_sid"] = sid
    char["owner_nick"] = room.participants[sid]["nickname"]
    # 换新卡时释放旧的
    previous_cid = room.participants[sid].get("char_id")
    if previous_cid and previous_cid in room.characters:
        room.characters[previous_cid]["owner_sid"] = None
        room.characters[previous_cid]["owner_nick"] = None
        room.persist_character(room.characters[previous_cid])
    room.add_character(char)
    room.participants[sid]["char_id"] = char["id"]
    state_mod.add_pc_to_combat(room, char)
    entry = room.add_log("system", "系统", f"{char['name']}（{char['owner_nick']}）加入了冒险！")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True, "id": char["id"], "redirect": "/play"})


@app.post("/api/delete_character")
async def api_delete_character(request: Request):
    """
    删除角色卡（不可恢复）：本人 / GM 席位 / 主机可删。
    同步解除会话绑定、从先攻条移除（校正指针）并删除磁盘文件，然后广播。
    """
    sid = participant_or_none(request)
    try:
        body = await request.json()
        cid = str(body.get("character_id", ""))
    except Exception:
        cid = ""
    char = room.characters.get(cid)
    if not char:
        return JSONResponse({"ok": False, "error": "角色不存在"}, status_code=404)
    allowed = is_host(request) or bool(sid and (sid == room.dm_sid or char.get("owner_sid") == sid))
    if not allowed:
        return JSONResponse({"ok": False, "error": "没有删除该角色的权限（本人 / GM / 主机）"}, status_code=403)
    name = char.get("name", "")
    state_mod.remove_character(room, cid)
    entry = room.add_log("system", "系统", f"角色卡「{name}」已被删除")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True, "name": name})


@app.post("/api/roll_abilities")
async def api_roll_abilities(request: Request):
    """建卡向导的"掷骰生成属性"：6 组 4d6 取三高。"""
    if not participant_or_none(request):
        return JSONResponse({"ok": False, "error": "会话已失效"}, status_code=400)
    return JSONResponse({"ok": True, "results": engine.roll_ability_scores()})


@app.post("/api/roll")
async def api_roll(request: Request):
    """无 WebSocket 时的掷骰兜底接口（同样走引擎并广播日志）。"""
    sid = participant_or_none(request)
    if not sid:
        return JSONResponse({"ok": False, "error": "请先输入昵称"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        result = engine.roll_expr(str(body.get("expr", "")))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    nickname = room.participants[sid]["nickname"]
    reason = str(body.get("reason", "")).strip()[:60]
    line = f"{nickname} 掷 {reason or '骰子'}：{result['breakdown']} = {result['total']}"
    entry = room.add_log("roll", nickname, line, meta=result)
    await hub.broadcast({"type": "log", "entry": entry})
    return JSONResponse({"ok": True, "result": result})


# ---------------------------------------------------------------------------
# WebSocket 端点
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """
    每个在线浏览器一条长连接：
      - 校验 sid 后注册进 Hub，立刻推一份快照
      - 循环接收消息交给 handle_message
      - 断开时更新在线状态并广播
    """
    sid = ws.cookies.get("sid")
    if not sid or sid not in room.participants:
        await ws.close(code=4401)
        return
    await ws.accept()
    was_connected = bool(room.participants[sid].get("connected"))
    hub.clients[sid] = ws
    room.participants[sid]["connected"] = True
    debuglog.log("ws", "connect", sid=sid, nickname=room.participants[sid].get("nickname"))
    # 以 WS 握手带的 cookie 判定主机身份（浏览器同源会自动带全量 cookie）
    room.participants[sid]["is_host"] = ws.cookies.get("host") == HOST_TOKEN
    if sid == room.dm_sid:
        # GM（重新）接入：让秘闻推送缓存失效，随后的快照广播会重推「GM 秘闻」
        _dm_brief_cache["sid"] = None
    await ws.send_text(json.dumps(snapshot_message(), ensure_ascii=False))
    if not was_connected:
        # 进入/重新加入：私发一条引擎生成的"当前状态"简报（仅本人可见；GM 会额外提示）
        briefing = {
            "id": f"brief-{int(time.time() * 1000)}",
            "ts": time.time(),
            "kind": "brief",
            "who": "系统",
            "text": state_mod.briefing_text(room, sid),
        }
        await ws.send_text(json.dumps({"type": "log", "entry": briefing}, ensure_ascii=False))
    await hub.broadcast(snapshot_message())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                await handle_message(sid, message)
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(sid, None)
        debuglog.log("ws", "disconnect", sid=sid)
        if sid in room.participants:
            room.participants[sid]["connected"] = False
        await hub.broadcast(snapshot_message())


# ---------------------------------------------------------------------------
# HTTP 路由：主机管理 API（全部要求主机 cookie）
# ---------------------------------------------------------------------------


@app.post("/api/manage/settings")
async def manage_settings(request: Request):
    """保存 LLM 配置 / 世界设定 / AI 主持额外要求（只更新提交了的字段）。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    llm = settings.setdefault("llm", {})
    if "base_url" in body:
        llm["base_url"] = str(body["base_url"]).strip()
    if "api_key" in body:
        llm["api_key"] = str(body["api_key"]).strip()
    if "model" in body:
        llm["model"] = str(body["model"]).strip()
    if "world" in body:
        room.world = str(body["world"])
    if "dm_note" in body:
        room.dm_note = str(body["dm_note"])
    state_mod.save_settings(settings)
    room.persist_room_meta()
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True})


@app.post("/api/manage/test_llm")
async def manage_test_llm(request: Request):
    """管理页"测试连接"按钮：实际发一条最小请求验证端点。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        reply = await ai.test_connection(settings)
        return JSONResponse({"ok": True, "reply": reply})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/manage/save")
async def manage_save(request: Request):
    """手动存档：把当前战役快照写入 data/saves/。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = state_mod.save_campaign(room, body.get("name", ""))
    entry = room.add_log("system", "系统", f"战役已手动存档：{name}")
    await hub.broadcast({"type": "log", "entry": entry})
    return JSONResponse({"ok": True, "name": name, "saves": state_mod.list_saves()})


@app.post("/api/manage/load")
async def manage_load(request: Request):
    """载入存档：覆盖当前战役（角色所有权与参与者选角会被重置）。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        payload = state_mod.load_campaign_file(body.get("name", ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"ok": False, "error": "存档文件损坏"}, status_code=400)
    room.load_campaign(payload)
    room.persist_room_meta()
    entry = room.add_log("system", "系统", f"战役已载入：{payload.get('name', '')}")
    await hub.broadcast(snapshot_message())
    await hub.broadcast({"type": "log", "entry": entry})
    return JSONResponse({"ok": True})


@app.post("/api/manage/mode")
async def manage_mode(request: Request):
    """主持模式总闸：无 GM 席位时是否由 AI 主持；切到 AI 会立即清空 GM 席位。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    room.ai_mode = bool(body.get("ai_mode"))
    cleared = False
    if room.ai_mode and room.dm_sid:
        room.dm_sid = None
        cleared = True
    room.persist_room_meta()
    await hub.broadcast(snapshot_message())
    if cleared:
        entry = room.add_log("system", "系统", "管理员切回 AI 主持，GM 席位已清空。")
        await hub.broadcast({"type": "log", "entry": entry})
    return JSONResponse({"ok": True})


@app.post("/api/manage/clear_dm")
async def manage_clear_dm(request: Request):
    """应急清空 GM 席位（GM 换设备/丢了 cookie 进不来时用）；日常由玩家自助接任与卸任。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    if not room.dm_sid:
        return JSONResponse({"ok": True})
    room.dm_sid = None
    room.persist_room_meta()
    entry = room.add_log("system", "系统", "管理员清空了 GM 席位（应急）。")
    await hub.broadcast(snapshot_message())
    await hub.broadcast({"type": "log", "entry": entry})
    return JSONResponse({"ok": True})


@app.post("/api/manage/reset_campaign")
async def manage_reset_campaign(request: Request):
    """
    重置战役进度（保留结构与角色卡，供同一战役从头重跑）：
    任务状态/注记、世界登记、压力、AI 摘要、游戏时钟、日志与战斗全部归零。
    """
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    if not campaign_mod.reset_progress(room.campaign):
        return JSONResponse({"ok": False, "error": "尚未设置战役结构"}, status_code=400)
    room.summary = ""
    room.last_summary_len = 0
    room.clock_minutes = 0.0
    room.log = []
    room.combat = {"active": False, "round": 0, "turn": 0, "combatants": []}
    room.persist_room_meta()
    entry = room.add_log("system", "系统", "战役进度已重置——冒险从起点重新开始（角色卡保留）。")
    await hub.broadcast(snapshot_message())
    await hub.broadcast({"type": "log", "entry": entry})
    return JSONResponse({"ok": True})


@app.post("/api/manage/debug")
async def manage_debug(request: Request):
    """运行时开关调试日志（本次进程有效；下次启动可用 run.py --debug 常开）。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if bool(body.get("enabled")):
        debuglog.enable()
        debuglog.log("system", "debug_enabled", by="manage")
    else:
        debuglog.log("system", "debug_disabled", by="manage")
        debuglog.disable()
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True, "enabled": debuglog.is_enabled(), "file": debuglog.current_file()})


@app.post("/api/debug/client")
async def debug_client_log(request: Request):
    """浏览器端错误上报入口（仅在调试模式开启时接受写入）。"""
    if not debuglog.is_enabled():
        return JSONResponse({"ok": False, "error": "调试未开启"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    debuglog.log("client", str(body.get("event", "report")), data=body.get("data"))
    return JSONResponse({"ok": True})


@app.get("/download/debug/{name}")
async def download_debug(request: Request, name: str):
    """下载某个调试日志文件（仅主机）。"""
    if not is_host(request):
        return RedirectResponse("/")
    path = debuglog.resolve_file(name)
    if path is None:
        return JSONResponse({"ok": False, "error": "日志文件不存在"}, status_code=404)
    return Response(
        path.read_text(encoding="utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={path.name}"},
    )


@app.get("/download/snapshot")
async def download_snapshot(request: Request):
    """下载当前房间完整状态快照（排查状态类问题用，仅主机）。"""
    if not is_host(request):
        return RedirectResponse("/")
    payload = room.campaign_payload()
    payload["participants"] = room.snapshot()["participants"]
    payload["debug_enabled"] = debuglog.is_enabled()
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        data,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=weavlum-snapshot.json"},
    )


@app.post("/api/manage/campaign/generate")
async def manage_campaign_generate(request: Request):
    """用 AI 生成战役结构草案；只把 JSON 文本返回给管理页，不直接入库。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        draft = await ai.generate_campaign_structure(settings)
        return JSONResponse({"ok": True, "draft": json.dumps(draft, ensure_ascii=False, indent=2)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/manage/campaign/save")
async def manage_campaign_save(request: Request):
    """校验并保存编辑后的结构；与既有运行时状态合并（保留任务状态/世界登记/压力）。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = str(body.get("json_text", "")).strip()
    if not raw:
        return JSONResponse({"ok": False, "errors": ["内容为空"]}, status_code=400)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "errors": [f"JSON 解析失败：{exc}"]}, status_code=400)
    errors = campaign_mod.validate_structure(data)
    if errors:
        return JSONResponse({"ok": False, "errors": errors[:20]}, status_code=400)
    room.campaign = campaign_mod.merge_campaign(room.campaign, data)
    room.persist_room_meta()
    entry = room.add_log("system", "系统", f"战役结构已更新：{room.campaign.get('title', '未命名')}")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True})


@app.post("/api/manage/campaign/clear")
async def manage_campaign_clear(request: Request):
    """清空战役结构（任务进度与压力一并移除；角色与日志不受影响）。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    room.campaign = None
    room.persist_room_meta()
    entry = room.add_log("system", "系统", "战役结构已清空。")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True})


@app.post("/api/manage/companion")
async def manage_companion(request: Request):
    """管理页"生成同伴"：引擎按职业推荐自动建卡（完整角色卡）并加入队伍。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        char = state_mod.create_companion(
            room,
            body.get("name", "") or "同伴",
            str(body.get("klass", "")),
            str(body.get("race", "") or "human"),
            body.get("level", 1),
            body.get("note", ""),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    entry = room.add_log("system", "系统", f"同伴「{char['name']}」（{char['race']}{char['klass']}）加入了队伍。")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True, "id": char["id"]})


@app.post("/api/manage/companion/remove")
async def manage_companion_remove(request: Request):
    """移除同伴（角色表、先攻条与磁盘一起清理）。"""
    if not is_host(request):
        return JSONResponse({"ok": False, "error": "需要主机权限"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        char = state_mod.remove_companion(room, body.get("character_id", ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    entry = room.add_log("system", "系统", f"同伴「{char['name']}」离队。")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# HTTP 路由：导出与导入
# ---------------------------------------------------------------------------


@app.get("/export/campaign")
async def export_campaign(request: Request):
    """导出整场战役为 JSON 文件（仅主机）。"""
    if not is_host(request):
        return RedirectResponse("/")
    payload = room.campaign_payload()
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        data,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=weavlum-campaign.json"},
    )


@app.get("/export/character/{cid}")
async def export_character(cid: str):
    """导出单张角色卡（weavlum-character 格式，可用于导入/换机）。"""
    char = room.characters.get(cid)
    if not char:
        return JSONResponse({"ok": False, "error": "角色不存在"}, status_code=404)
    payload = {
        "format": "weavlum-character",
        "version": 1,
        "character": state_mod.public_character(char),
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        data,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=character-{cid}.json"},
    )


@app.post("/api/import_character")
async def import_character(request: Request, file: UploadFile = File(...)):
    """
    导入角色卡并直接入场：
    接受 weavlum-character 包装格式或裸角色卡 dict；
    只做必要字段校验，其余字段信任原文件（同平台导出的数据）。
    """
    sid = participant_or_none(request)
    if not sid:
        return JSONResponse({"ok": False, "error": "请先输入昵称"}, status_code=400)
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"ok": False, "error": "文件不是有效的 JSON"}, status_code=400)
    if isinstance(payload, dict) and payload.get("format") == "weavlum-character":
        char = payload.get("character")
    else:
        char = payload
    required = ("name", "abilities", "mods", "hp", "ac")
    if not isinstance(char, dict) or not all(key in char for key in required):
        return JSONResponse({"ok": False, "error": "不是有效的角色卡文件"}, status_code=400)
    char = dict(char)
    state_mod.migrate_character_fields(char)
    char["id"] = None
    char["owner_sid"] = sid
    char["owner_nick"] = room.participants[sid]["nickname"]
    room.add_character(char)
    room.participants[sid]["char_id"] = char["id"]
    state_mod.add_pc_to_combat(room, char)
    entry = room.add_log("system", "系统", f"{char['name']} 的角色卡已导入并入场。")
    await hub.broadcast({"type": "log", "entry": entry})
    await hub.broadcast(snapshot_message())
    return JSONResponse({"ok": True, "id": char["id"]})
