"""
LLM 接入层：AI GM 的大脑接口 + 全部工具（function calling）的实现。

本模块分四块：
  1. TOOLS           —— 暴露给模型的工具定义（掷骰/查怪/生成怪/移动/回合/HP/看队伍）
  2. 简报组装        —— board_brief（战术板距离）、party_brief（队伍数值）：
                        所有给模型看的数值都来自引擎，模型只能引用、不能编造
  3. 提示词          —— build_system_prompt：主持准则 + 遭遇预算 + 世界/摘要/附加要求
  4. 对话循环        —— generate_reply：调用 OpenAI 兼容接口，执行工具调用并回喂结果，
                        最多 6 轮，最后一轮强制不带工具输出正文；
                        另有 maybe_update_summary（滚动剧情摘要）与 test_connection

设计原则：模型是"叙事与决策者"，引擎是"数值权威"——凡随机数、坐标、HP、
先攻都必须经过这里的工具落到 state/engine 函数上。
"""

import json
import re
import time

import httpx

from . import actions as actions_mod, campaign as campaign_mod, debuglog
from . import rules_ref as rules_ref_mod
from . import state as state_mod

# ---------------------------------------------------------------------------
# 工具定义（发送给模型的 function schemas；描述同时也是对模型的约束说明）
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": "掷骰。任何随机数都必须通过此工具产生，禁止自行编造结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "掷骰表达式，例如 1d20+5、2d6+3"},
                    "reason": {"type": "string", "description": "掷骰原因，例如：力量豁免、先攻"},
                    "dc": {"type": "integer", "description": "可选。若这是对抗 DC 的检定，传入 DC 值"},
                    "mode": {
                        "type": "string",
                        "enum": ["normal", "advantage", "disadvantage"],
                        "description": "可选：优势/劣势（仅对单个 d20 表达式生效）。带优/劣势时禁止连掷两次当两次结果。",
                    },
                },
                "required": ["expression", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_party",
            "description": "查看队伍成员的当前状态（HP、AC、职业等级）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_monsters",
            "description": "查询怪物图鉴（真实 AC/HP/攻击/CR/XP）。设计遭遇或战斗前必须先查询，禁止凭空编造怪物数值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "可选：按名字过滤（中英文皆可）"},
                    "max_cr": {"type": "number", "description": "可选：只返回 CR 不超过该值的怪物"},
                    "min_cr": {"type": "number", "description": "可选：只返回 CR 不低于该值的怪物（与 max_cr 组合即 CR 区间筛选）"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_monsters",
            "description": "把怪物加入先攻条（自动掷先攻；战斗未开始会先自动开始）。只能用 query_monsters 返回的 id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "list": {
                        "type": "array",
                        "description": "要生成的怪物",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "怪物 id，例如 goblin"},
                                "count": {"type": "integer", "description": "数量，默认 1"},
                            },
                            "required": ["id"],
                        },
                    },
                },
                "required": ["list"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_combatant_toward",
            "description": "让一个战斗者（玩家角色或怪物）向另一个战斗者移动（战术板棋子同步；引擎按移动速度与实际距离截断，近战停在 5 尺外）。任何移动都必须调用此工具，不要只在叙述里移动。玩家角色只能在其回合移动，否则先调用 advance_turn。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mover": {"type": "string", "description": "移动者 id 或名字（如 Apolynoid、狗头人；同名多只时自动选最接近目标的）"},
                    "target": {"type": "string", "description": "目标 id 或名字"},
                    "feet": {"type": "number", "description": "希望移动的尺数（默认 30）；引擎会按剩余移动力截断"},
                },
                "required": ["mover", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_turn",
            "description": "把先攻条推进到下一位行动者。每当一名行动者结束行动、轮到下一位时调用，保持先攻条与叙述一致。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_hp",
            "description": "对角色或怪物造成伤害/治疗（引擎结算并同步角色卡与战术板 HP）。攻击命中后的伤害必须用此工具结算，不要只在叙述里改 HP；未命中的攻击不要掷伤害、也不要调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "角色名/id 或怪物名/id"},
                    "delta": {"type": "integer", "description": "HP 变化：伤害传负数（如 -5），治疗传正数"},
                    "reason": {"type": "string", "description": "可选：原因（如 狗头人匕首）"},
                },
                "required": ["target", "delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_campaign",
            "description": "读取当前战役结构（任务清单/进度/世界登记）。开新场景或不确定任务细节时先读它。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_quest",
            "description": "更新任务状态。玩家取得实质进展时置 in_progress；达成完成条件置 completed（会自动推进压力条）；彻底失败置 failed。可附一行进展注记。",
            "parameters": {
                "type": "object",
                "properties": {
                    "quest": {"type": "string", "description": "任务 id 或标题"},
                    "status": {
                        "type": "string",
                        "enum": ["undiscovered", "in_progress", "completed", "failed", "expired"],
                    },
                    "note": {"type": "string", "description": "可选：一行进展注记，会显示在任务板上"},
                },
                "required": ["quest", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_world_note",
            "description": "登记世界笔记（玩家遇到的新地点、NPC、线索），保证后续剧情连贯。同名条目会追加而不是新建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["locations", "npcs", "clues"]},
                    "name": {"type": "string", "description": "地点/NPC/线索的名字"},
                    "note": {"type": "string", "description": "一两句关键信息"},
                },
                "required": ["kind", "name", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_pressure",
            "description": "额外推进压力条一格（用于玩家无视代价、拖延或世界恶化的情形；任务完成已自动推进，不要重复调用）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "推进原因（会写进播报）"},
                    "amount": {"type": "integer", "description": "可选：推进格数，默认 1，最多 6"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cast_spell",
            "description": "施放法术（引擎校验已知/消耗法术位/掷伤害或治疗骰）。任何施法都必须调用此工具；伤害或治疗再由你调用 adjust_hp 结算到具体目标；升环施法传更高的 slot_level。",
            "parameters": {
                "type": "object",
                "properties": {
                    "caster": {"type": "string", "description": "施法者角色名或 id"},
                    "spell": {"type": "string", "description": "法术 id 或名字（中文/英文均可）"},
                    "slot_level": {"type": "integer", "description": "可选：使用的法术位环阶（默认等于法术本身环阶）"},
                },
                "required": ["caster", "spell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_spell",
            "description": "让角色习得/添加法术到指定栏位（BFRD 分层：戏法 / 已知（已知型职业）/ 准备（准备型职业）/ 法术书（法师，消耗每级 2 条免费名额）/ 仪式）。bucket 省略时按职业与环阶自动推断；受来源、环阶与各栏位上限校验。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "spell": {"type": "string", "description": "法术 id 或名字"},
                    "bucket": {"type": "string", "enum": ["cantrips", "known", "prepared", "spellbook", "rituals"], "description": "目标栏位（可选：不传则自动）"},
                    "free": {"type": "boolean", "description": "法师法术书专用：true=消耗免费名额（默认），false=按卷轴抄录（不计名额，GM 裁定）"},
                },
                "required": ["character", "spell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swap_spell",
            "description": "替换已知法术（BFRD：吟游/术士/邪术师/圣武士/游侠 每升 1 级获得 1 个替换名额）：旧法术 → 新法术（来源/环阶/去重校验）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "old": {"type": "string", "description": "要替换掉的已知法术（id 或名字）"},
                    "spell": {"type": "string", "description": "换入的新法术（id 或名字）"},
                },
                "required": ["character", "old", "spell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_spell",
            "description": "让角色遗忘一个已学法术（腾出数量名额，供 learn_spell 换用法术）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "spell": {"type": "string", "description": "要遗忘的法术 id 或名字"},
                },
                "required": ["character", "spell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_effect",
            "description": "给角色挂一个状态效果（法术效果由 cast_spell 自动处理；这里用于闪避、狂暴等非施法状态）。专注类效果会替换旧的专注。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "角色名或 id"},
                    "name": {"type": "string", "description": "效果名，如：闪避"},
                    "rounds": {"type": "integer", "description": "持续轮数（默认 1；0 表示直到手动移除）"},
                    "concentration": {"type": "boolean", "description": "可选：是否属于专注"},
                    "note": {"type": "string", "description": "可选：说明"},
                },
                "required": ["target", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_effect",
            "description": "移除角色身上的一个状态效果（按名字）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "角色名或 id"},
                    "name": {"type": "string", "description": "效果名（可部分匹配）"},
                },
                "required": ["target", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "level_up",
            "description": "让角色升 1 级（里程碑达成时使用：某阶任务全部完成后按 update_quest 的提示调用）。引擎自动计算 HP/熟练/法术位，等级上限 20；3 级解锁子职业（choose_subclass）、4/8/12/16/19 级获得提升（先问玩家再 apply_asi）、10/20 级获得恩赐（先问玩家再 choose_boon）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                },
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_asi",
            "description": "为刚获得提升（Improvement）的角色分配（4/8/12/16/19 级各一次）。三种方式：+2 一项 / +1 两项 / 一项 +1 并选一项本职业提升天赋（talent）。执行前先询问玩家选哪种；同伴由系统自动分配，不要代玩家选择。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "plus_two": {"type": "string", "description": "单项 +2 的属性（str/dex/con/int/wis/cha）"},
                    "plus_one": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "各 +1 的属性：两项（合计 +2）或一项加天赋（合计 +1）",
                    },
                    "talent": {"type": "string", "description": "选择「一项 +1 + 天赋」时给出的天赋 id（必须在本职业提升天赋列表内）"},
                },
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "choose_subclass",
            "description": "为 3 级的角色选择子职业（引擎校验子职业属于本职业）。先问玩家想要哪个子职业，再调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "subclass": {"type": "string", "description": "子职业 id（见角色卡的待选提示；如 berserker、life-domain）"},
                },
                "required": ["character", "subclass"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "choose_boon",
            "description": "为 10 级（英雄恩赐）或 20 级（史诗恩赐）的角色选择恩赐。先问玩家想要哪一项，再调用；level 省略时取最小待选等级。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "level": {"type": "integer", "description": "10 或 20；省略则取最小待选等级"},
                    "boon": {"type": "string", "description": "恩赐选项的 key（角色卡待选提示中的序号字符串）"},
                },
                "required": ["character", "boon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recruit_companion",
            "description": "招募一名 NPC 同伴（引擎按职业推荐自动生成完整角色卡并加入队伍；战斗中自动进入先攻条，由你代为行动）。给出名字与职业即可；NPC 主动加入剧情时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "同伴名字"},
                    "klass": {"type": "string", "description": "职业 id（如 fighter、cleric、rogue）"},
                    "race": {"type": "string", "description": "可选：种族 id，默认 human"},
                    "level": {"type": "integer", "description": "可选：初始等级（默认 1；建议与队伍持平）"},
                    "note": {"type": "string", "description": "可选：一句话性格/背景设定"},
                },
                "required": ["name", "klass"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_time",
            "description": "推进游戏时间（分钟）。旅行、搜索、扎营、等待等耗时行为都要调用——限时法术与效果会随时钟到期。",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "推进的分钟数（如搜索房间 10、短途赶路 30）"},
                    "reason": {"type": "string", "description": "原因（会写进播报）"},
                },
                "required": ["minutes", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "short_rest",
            "description": "全队短休（1 小时）：推进时钟并结算效果到期（法师「奥术恢复」由玩家在角色卡自行处理）。玩家说要短休时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "long_rest",
            "description": "全队长休（8 小时）：推进时钟、结算效果到期；全员满 HP、法术位全恢复、效果与专注清空。玩家说要长休/过夜时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_combat",
            "description": "结束当前战斗（玩家脱离、谈判成功、敌人逃走或换场景时调用）。倒地的怪物由引擎自动移出先攻条，敌人全灭时引擎也会自动结束——这两种情况无需调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "结束原因（会写进播报）"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inventory",
            "description": "查看某角色的金币与背包物品。",
            "parameters": {
                "type": "object",
                "properties": {"character": {"type": "string", "description": "角色名或 id"}},
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_item",
            "description": "把物品放进角色背包（战利品/报酬/礼物必须记账）。item 用物品库 id 或中文名（如 potion-of-healing / 治疗药水）；自定义物品可直接给名称并附 note 描述。同名且同备注自动合并数量，备注不同分开存放。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "item": {"type": "string", "description": "物品库 id 或物品名"},
                    "qty": {"type": "integer", "description": "数量，默认 1"},
                    "note": {"type": "string", "description": "可选备注/描述（自定义装备说明、药水品质等）"},
                },
                "required": ["character", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_item",
            "description": "从角色背包移除物品（消耗/丢弃/被偷）。喝掉药水、用掉卷轴这类整件消耗也要调用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "item": {"type": "string", "description": "物品 id 或名称"},
                    "qty": {"type": "integer", "description": "数量，默认 1"},
                },
                "required": ["character", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_gold",
            "description": "增减角色资产（发放报酬、扣费、罚款）。单位为金（gp，可小数，如 0.5 = 5 银）；系统自动按 10 铜=1 银、10 银=1 金、10 金=1 铂 并档与找零；扣减超过持有会失败。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "delta": {"type": "number", "description": "资产增减值（金），例如 10 或 -2.5"},
                },
                "required": ["character", "delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buy_item",
            "description": "角色从商店购买物品：按库价扣金币并入库（金币不足会失败）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "item": {"type": "string", "description": "物品库 id 或中文名"},
                    "qty": {"type": "integer", "description": "数量，默认 1"},
                },
                "required": ["character", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_item_note",
            "description": "给背包物品写/改/清备注（品质、来历、自定义描述等）。item 支持 id/名称/子串；同名多条时可用 old_note 精确定位。留空 note 表示清除备注。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "item": {"type": "string", "description": "背包中的物品 id 或名称"},
                    "note": {"type": "string", "description": "备注内容（留空清除）"},
                    "old_note": {"type": "string", "description": "可选：原备注，用于区分同名物品"},
                },
                "required": ["character", "item", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sell_item",
            "description": "角色出售背包物品：按库价半价回收金币（仅限物品库中有定价的物品）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "item": {"type": "string", "description": "背包中的物品 id 或名称"},
                    "qty": {"type": "integer", "description": "数量，默认 1"},
                },
                "required": ["character", "item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grant_luck",
            "description": "授予幸运（BFRD 幸运系统）：PC 攻击未命中/豁免失败时每回合一次 +1（from_failure=true）；GM 可因聪明点子/出色扮演/艰难遭遇嘉奖任意点数。满 5 点再获得时会掷 d4 重置（幸运之触为 1d4+1），引擎自动处理。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "amount": {"type": "integer", "description": "授予点数，默认 1"},
                    "reason": {"type": "string", "description": "原因（写入日志）"},
                    "from_failure": {"type": "boolean", "description": "因失败积累（触发幸运之触翻倍）"},
                },
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spend_luck",
            "description": "消耗幸运：1 点 = 任何检定 +1 加值；3 点 = 检定后重掷一个 d20（不能防大失败、不能造重击）。引擎校验余额并扣减。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "amount": {"type": "integer", "description": "消耗点数（1 加值 / 3 重掷）"},
                    "reason": {"type": "string", "description": "用途说明（写入日志）"},
                },
                "required": ["character", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_check",
            "description": "技能/属性检定（引擎自动取调整值与熟练、自动处理力竭劣势）。任何需要技能判定（察觉/隐匿/运动/医药/说服……）或纯属性检定时优先用它，不要自己拼加值。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "检定者（角色名或 id）"},
                    "skill": {"type": "string", "description": "技能 id（如 perception/stealth/athletics/medicine/persuasion）"},
                    "ability": {"type": "string", "description": "纯属性检定时的属性（str/dex/con/int/wis/cha），与 skill 二选一"},
                    "dc": {"type": "integer", "description": "难度等级（可选；不传则只出数值）"},
                    "mode": {"type": "string", "enum": ["normal", "advantage", "disadvantage"], "description": "优势/劣势"},
                },
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stabilize",
            "description": "稳定化：救助者以 DC 10 感知（医药）检定稳定一个 0 HP 的濒死角色（成功者 1d4 小时后自动回复 1 HP）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "濒死目标（角色名或 id）"},
                    "by": {"type": "string", "description": "实施救助的角色（默认目标自己，一般应传同伴）"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revive",
            "description": "复活：施法者消耗对应法术位施展复活法术（复苏术/死者复活/复活术/真实复活术），让已死亡角色回复 1 HP 重返人世（仍倒地）。普通治疗对死者无效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "caster": {"type": "string", "description": "施法者（需已知复活法术）"},
                    "character": {"type": "string", "description": "已死亡的目标"},
                    "spell": {"type": "string", "description": "指定法术 id（可选，默认自动选已知的最低环复活法术）"},
                },
                "required": ["caster", "character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_exhaustion",
            "description": "增减力竭等级（BFRD 六档：1 检定劣势 / 2 速度减半 / 3 攻击豁免劣势 / 4 HP 上限减半 / 5 速度 0 / 6 死亡）。饥渴、酷寒酷热、强行军、法术效果等按其描述调用；引擎自动应用数值效果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "delta": {"type": "integer", "description": "变化量（+1 获得一级 / -1 移除一级）"},
                    "reason": {"type": "string", "description": "原因（写入日志）"},
                },
                "required": ["character", "delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "downtime",
            "description": "休整期活动（BFRD 冒险之间）：引擎自动收钱、推进时钟、掷骰查表、记账。activity：carousing 狂欢（tier=lower/middle/upper，每周 10/50/250 金，游说检定→人脉）/ researching 研究（每周 50 金，智力技能检定→线索）/ crafting 制作（每天 10 金进度、材料半价，完成后自动入包）/ training 训练（每周 1 金，d20+PB 查表→熟练/天赋进度）/ working 工作（技能检定→周薪）。weeks 默认 1。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "activity": {"type": "string", "enum": ["carousing", "researching", "crafting", "training", "working"]},
                    "weeks": {"type": "integer", "description": "持续周数（狂欢/研究/训练/工作）"},
                    "days": {"type": "integer", "description": "制作天数"},
                    "tier": {"type": "string", "enum": ["lower", "middle", "upper"], "description": "狂欢对象阶级"},
                    "skill": {"type": "string", "description": "研究（arcana/history/nature/religion）或工作的技能 id"},
                    "topic": {"type": "string", "description": "研究主题（叙事用）"},
                    "goal": {"type": "string", "description": "训练目标（要学的熟练/天赋）"},
                    "item": {"type": "string", "description": "制作的物品（库内 id/中文名）"},
                    "price_cp": {"type": "integer", "description": "自定义物品的成品总价（铜）"},
                },
                "required": ["character", "activity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wear_armor",
            "description": "更换穿着（信任制）：从背包里的护甲条目选择穿戴或脱下行头（盾牌独立开关），引擎重算 AC。玩家说「我换上/脱下某甲/盾」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "armor": {"type": "string", "description": "护甲 id 或名称（背包里有的；传 none 表示脱下）"},
                    "shield": {"type": "boolean", "description": "是否持盾（省略则保持现状）"},
                },
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spend_hit_dice",
            "description": "短休时消耗生命骰回血（BFRD）：每颗掷 dX + 体质调整值，数量上限为角色等级；长休自动恢复一半。玩家说「我用生命骰回血/短休回血」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "角色名或 id"},
                    "count": {"type": "integer", "description": "消耗颗数（默认 1）"},
                },
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "damage_vehicle",
            "description": "载具受损/恢复：单次伤害低于载具伤害阈值时可传 single_hit=true 判定不受损；HP 归零即摧毁。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "持有载具的角色"},
                    "vehicle": {"type": "string", "description": "载具 id 或名称"},
                    "delta": {"type": "integer", "description": "HP 变化（负数受损）"},
                    "single_hit": {"type": "boolean", "description": "按单次命中判定伤害阈值"},
                },
                "required": ["character", "vehicle", "delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_vehicle",
            "description": "修理载具（BFRD）：每小时消耗基础价 10% 的部件、恢复等于 AC 值的 HP；被摧毁（0 HP）的载具无法修理。",
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {"type": "string", "description": "持有载具的角色"},
                    "vehicle": {"type": "string", "description": "载具 id 或名称"},
                    "hours": {"type": "integer", "description": "修理小时数（默认 1）"},
                },
                "required": ["character", "vehicle"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 请求辅助：从 settings（data/settings.json）里取端点/鉴权/模型
# ---------------------------------------------------------------------------


def _endpoint(settings):
    """拼出 chat/completions 地址；base_url 支持带或不带 /v1。"""
    base = (settings.get("llm", {}).get("base_url") or "").rstrip("/")
    if not base:
        raise ValueError("未配置 LLM base_url")
    return base + "/chat/completions"


def _headers(settings):
    """OpenAI 兼容鉴权头；本地 Ollama 可以留空 key。"""
    headers = {"Content-Type": "application/json"}
    key = settings.get("llm", {}).get("api_key") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _model(settings):
    return settings.get("llm", {}).get("model") or "deepseek-chat"


# ---------------------------------------------------------------------------
# 给模型看的"事实简报"：全部来自引擎数据
# ---------------------------------------------------------------------------

# 渲染器（party_brief / board_brief / 怪物查询 / 生成文本）已迁至动作层（server/actions.py），
# 保证「系统第一公民」：AI、人类 GM 控制台与玩家消息共用同一套动作实现。


def _board_suffix(room):
    """工具结果后附的最新战术板（让模型立刻看到变化后的距离）。"""
    board = actions_mod.board_brief(room)
    return ("\n" + board) if board else ""


def build_system_prompt(room):
    """
    拼出完整的 system prompt：角色 + 主持准则 + 遭遇预算 + 战术板
    + 世界设定 + 剧情摘要 + 主机额外要求 + 队伍简报。
    顺序即优先级参考，越靠后的越具体。
    """
    if room.ai_mode and not room.dm_sid:
        role = "你是这场《黑旗 SRD》（Black Flag Roleplaying，OSR 风味的中世纪奇幻）战役的游戏主持人（GM）：描述世界、扮演 NPC、裁定规则与掷骰，但把行动的权力留给玩家。"
    else:
        role = "本战役由人类 GM 主持。你是 GM 的 AI 助手：回答规则问题、扮演 NPC、提供描述，不要主动推进剧情主线。"
    # 主持准则是"桌面体验"的核心约束：节奏、篇幅、公平、死局、开局、数值纪律
    guidelines = (
        "【主持准则】\n"
        "1. 简短克制：每次回复正文不超过 150 字，最多一小段；描写生动即可，严禁堆砌文戏或连续独白。\n"
        "2. 一回合一个节拍：只处理玩家本次行动的直接结果，给出眼前的情境、目标或抉择，然后停下；不要一次演完大段剧情。\n"
        "3. 交还行动权：每段结尾用一句话把球踢回给玩家（如「你打算怎么做？」），除非你正在等掷骰结果。\n"
        "4. 公平有挑战但绝不设死局：按队伍等级设计难度（1 级角色的血量与能力有限）；绝不出现「无论怎么做都打不过/进不去」的障碍，也绝不放置必败战斗。如果剧情需要强力 NPC，只能让其构成压力，玩家必须看得出绕开、谈判、智取或逃跑的路。\n"
        "5. 失败也推进：检定失败意味着代价、麻烦或新线索，而不是卡死剧情；不要让一次坏骰子毁掉整场冒险。\n"
        "6. 开局：如果日志里还没有你的开场叙述，用不超过 80 字给出具体、有钩子的开场（一个地点、一个人、一件亟待解决的事），然后直接把选择交给玩家；不要科普世界观。\n"
        "7. 检定与数值：需要判定时调用 roll_dice，DC 取 10-20 之间的合理值；轮到某个角色时，使用队伍简报中该角色的对应加值构造表达式（例如察觉+2 → 1d20+2），不要猜测数值；任何随机数（含攻击、伤害、豁免）都必须由 roll_dice 产生，绝不自行编造；优势/劣势必须传 mode 参数（引擎按规则取高/低），严禁连掷两次当作两次结果；攻击命中后的伤害必须用 adjust_hp 结算（角色或怪物），未命中不要掷伤害。\n"
        "8. 语言：使用中文、第二人称叙述；不提及「系统」「引擎」「AI」等现实概念；不替玩家决定行动。\n"
        "9. 遭遇与战斗：设计战斗前先用 query_monsters 查询真实怪物数据，按下方预算选怪；战斗开场必须用 spawn_monsters 把怪物加入先攻条（引擎自动掷先攻）；任何移动（玩家角色的声明移动、怪物接近）都必须用 move_combatant_toward 同步战术板；每当一名行动者结束行动、轮到下一位时，用 advance_turn 推进先攻条；若先攻条接下来是怪物或同伴的回合，必须在同一次回复内依次处理完（同伴由你代为行动；各自动结后 advance_turn），直到轮到玩家角色才可以交还话语权；攻击结算用 roll_dice。怪物倒地由引擎自动移出先攻条、全灭自动结束战斗；玩家脱离/谈判等收尾用 end_combat；开新一场战斗会自动重置站位与先攻。\n"
        "10. 战役结构：任务的地点/目标/关键转折必须与【战役结构】一致；玩家取得实质进展时用 update_quest 更新任务状态（完成任务会自动推进压力条，不要重复调用 advance_pressure）；遇到新地点/NPC/线索用 add_world_note 登记；玩家无视代价或世界恶化时用 advance_pressure 额外推进；终局条件满足时引导玩家走向终局地点。\n"
        "11. 法术与升级：任何施法都必须调用 cast_spell（引擎校验已知/扣法术位/掷伤害或治疗；升环传更高的 slot_level）；伤害与治疗再由 adjust_hp 结算到具体目标；闪避、狂暴等非施法状态用 add_effect/remove_effect；换法术用 learn_spell / forget_spell（受职业与数量限制）；某阶任务全部完成（update_quest 会提示）时为每个角色（含同伴）调用 level_up（等级上限 20）；升级到 3 级会解锁子职业（先问玩家选哪个再 choose_subclass）；4/8/12/16/19 级获得提升（Improvement）——先问玩家选「+2 一项 / +1 两项 / +1 一项加一项本职业提升天赋」再 apply_asi；10/20 级获得恩赐（先问玩家选择再 choose_boon）；同伴由系统自动结算上述选择；NPC 要加入队伍时用 recruit_companion（给出名字与职业即可）；法术位与已知法术的实时数据在队伍简报里，不要编造。\n"
        "12. 幸运：PC 攻击未命中或豁免失败时（每回合一次）用 grant_luck（from_failure=true）记 1 点；你也可因聪明点子/精彩扮演/有趣选择奖励 1 点，或按难度在遭遇后奖励数点。玩家要用幸运时用 spend_luck（1 点 = 检定 +1；3 点 = 重掷一个 d20；不能防大失败、不能造重击）。幸运上限 5 点（超出时引擎掷 d4 重置，幸运之触为 1d4+1）。把幸运当作叙事资源：先问玩家是否消耗，再掷骰。\n"
        "13. 时间与休息：旅行、搜索、扎营等耗时行为必须调用 advance_time 推进游戏时间（限时法术与效果会随时钟到期）；玩家说要短休/长休时调用 short_rest / long_rest（引擎结算 HP 恢复、法术位与效果到期），叙事与游戏时钟保持一致。\n"
        "14. 物品与金钱：战利品/报酬/礼物一律用 add_item 入账（物品库 id 或中文名；自定义物品直接给名）；金钱增减用 adjust_gold；购买用 buy_item、出售用 sell_item（半价回收，自定义物品不可自动定价）；整件消耗（喝下的药水、用掉的卷轴）用 remove_item。自定义物品/特殊品质用 note 备注（add_item 的 note 参数，或 set_item_note 修改已有物品；如「上等治疗药水」）。绝不允许只叙述「你获得了……/你花掉……」而不调用工具记账。\n"
        "15. 濒死与死亡（引擎自动结算，你只叙述与响应）：角色降到 0 HP 会自动进入濒死（自动挂昏迷/倒地状态）；其回合开始引擎自动掷死亡豁免；0 HP 再受伤自动记失败（重击两次）；三次失败即死亡、三次成功变稳定（1d4 小时后自动苏醒）；单次溢出伤害 ≥ HP 上限当场死亡。你不需要手动结算这些——如实叙述结果即可。稳定濒死用 stabilize；角色死亡后普通治疗无效，复活用 revive（需施法者已知复活法术与法术位）。\n"
        "16. 技能检定与休整期：任何技能/属性检定优先用 skill_check（引擎自动取调整值与熟练、自动处理力竭劣势），不要自己拼 d20 表达式；Downtime（狂欢/研究/制作/训练/工作）用 downtime 动作（引擎自动收钱、推进时钟、掷骰查表）；力竭增减用 adjust_exhaustion；载具受伤/修理用 damage_vehicle / repair_vehicle；短休回血用 spend_hit_dice（消耗生命骰，每颗 dX+体质；长休自动恢复一半）；换装用 wear_armor（AC 由引擎按所穿护甲数据重算）。\n"
        "17. 法术栏位（BFRD 分层）：角色升到新等级后法术容量会增长——法师每级有 2 条法术书免费名额（learn_spell bucket=spellbook）、已知型职业每级可替换 1 条已知法术（swap_spell）、新增戏法/已知/仪式名额；升级播报会列出变化清单，请据此提醒玩家分配（或由你按玩家意愿代办）。戏法与法术不同栏；法师施法前必须把法术放进准备列表（prepared）；仪式施法要求法术在仪式栏位；一键清点见队伍简报的「法术」行。"
    )
    # 遭遇强度：Black Flag 无 XP 预算，用 CR 粗估口径（队伍人数 + 等级差）
    pc_count = sum(1 for char in room.characters.values() if char.get("owner_sid")) or 0
    encounter = (
        "【遭遇设计（硬性）】\n"
        f"当前队伍人数：{pc_count or 1} 人。\n"
        "Black Flag 没有 XP 预算表，按 CR 粗估难度（怪物数据以 query_monsters 结果为准，禁止编造）：\n"
        "- 单只怪物：CR ≈ 队伍平均等级 → 中等；低 2 级及以上 → 轻松；高 1-2 级 → 困难；高 3 级以上 → 致命（仅剧情高潮，且必须留撤退/谈判/绕开的路）。\n"
        "- 多只怪物：2 只 ≈ 单只 CR+1 的压力；3-4 只 ≈ CR+2；5 只以上改用低 2-3 级的怪群。\n"
        "- 人数不足 4 人按比例减数量（下调怪物只数而不是抬高 CR）；人数多则加数量。\n"
        "- 怪物含特性/巢穴/区域效应数据，栖息地型遭遇优先用符合地形的怪。\n"
        "- 战斗胜利不发 XP：进度按任务里程碑升级（update_quest 的提示触发 level_up）。"
    )
    parts = [role, guidelines, encounter]
    # 规则速查（防止翻规则书）：未机制化或半机制化章节的原文浓缩 + 状态表 + 神祇表
    parts.append("【规则速查（Black Flag 原文浓缩，数值以本表与引擎数据为准，不要另行发明）】\n"
                 + rules_ref_mod.rules_reference_text())
    parts.append("【状态速查（16 条，施加/结算状态时照此裁定）】\n" + actions_mod.conditions_brief())
    parts.append("【神祇速查（设定与信仰用，可自由混搭）】\n" + actions_mod.gods_brief())
    if room.campaign:                  # 主机已生成战役结构时，每轮注入作为硬约束
        parts.append(campaign_mod.render_brief(room.campaign))
    parts.append(f"【游戏时间】{state_mod.format_clock(room.clock_minutes)}")
    board = actions_mod.board_brief(room)   # 战斗中才有战术板段落
    if board:
        parts.append(board)
    if room.world.strip():             # 主机粘贴的世界设定（最长 4000 字）
        parts.append("【世界设定 / 模组】\n" + room.world.strip()[:4000])
    if room.summary.strip():           # 滚动摘要（长战役记忆），最长 4000 字
        parts.append("【剧情摘要（早期剧情，供你保持连贯）】\n" + room.summary.strip()[:4000])
    if room.dm_note.strip():           # 主机的额外主持要求，优先级最高
        parts.append("【主机的额外要求（优先级高于以上风格约定）】\n" + room.dm_note.strip()[:1000])
    parts.append("【当前队伍】\n" + actions_mod.party_brief(room))
    features_brief = actions_mod.party_features_brief(room)
    if features_brief:
        parts.append("【队伍规则资料（职业/子职业特性与血统传承原文；运行特性时按此执行）】\n" + features_brief)
    return "\n\n".join(parts)


def log_to_messages(room, limit=30):
    """
    把最近 limit 条日志转换成对话消息：
    玩家发言→user，AI 叙述→assistant，掷骰/系统→user（带 [..] 前缀）。
    """
    messages = []
    for entry in room.log[-limit:]:
        kind = entry.get("kind")
        if kind == "chat":
            messages.append({"role": "user", "content": f"玩家「{entry['who']}」：{entry['text']}"})
        elif kind == "narrate":
            messages.append({"role": "assistant", "content": entry["text"]})
        elif kind == "roll":
            messages.append({"role": "user", "content": f"[掷骰记录] {entry['text']}"})
        elif kind == "system":
            messages.append({"role": "user", "content": f"[系统] {entry['text']}"})
    return messages


# ---------------------------------------------------------------------------
# 主对话循环：调用模型 + 执行工具 + 收集结果
# ---------------------------------------------------------------------------


async def generate_reply(settings, room):
    """
    向 LLM 要一次 GM 回复。

    循环最多 6 轮：
      - 每轮把当前消息历史发给模型；若模型返回 tool_calls 就逐个执行，
        把引擎结果作为 role=tool 消息回喂，继续下一轮
      - 最后一轮强制不带工具，并要求模型直接输出正文（防止无限调工具）
    返回 (正文, 掷骰记录列表, 需要写日志的事件列表)：
      rolls 由 main.ai_reply_task 记入日志广播；notes 同理（移动/HP 结算等）。
    """
    messages = [{"role": "system", "content": build_system_prompt(room)}] + log_to_messages(room)
    rolls = []
    notes = []
    max_rounds = 6
    # 调试日志：记录本次生成的系统提示词与历史消息（打码/截断由 debuglog 处理）
    debuglog.log("ai", "start", model=_model(settings), system=messages[0]["content"], history=messages[1:])
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=180) as client:
        for round_index in range(max_rounds):
            last_round = round_index == max_rounds - 1
            if last_round:
                # 最后一轮：补一条强指令 + 不携带 tools，逼模型出正文
                messages.append({"role": "user", "content": "[系统] 请立刻用一段正文回复玩家，不要再调用任何工具。"})
            payload = {"model": _model(settings), "messages": messages}
            if not last_round:
                payload["tools"] = TOOLS
                payload["tool_choice"] = "auto"
            response = await client.post(_endpoint(settings), headers=_headers(settings), json=payload)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            debuglog.log(
                "ai",
                "round",
                index=round_index,
                ms=round((time.monotonic() - started) * 1000),
                content=message.get("content"),
                tool_calls=[
                    {
                        "name": call.get("function", {}).get("name"),
                        "arguments": call.get("function", {}).get("arguments"),
                    }
                    for call in tool_calls
                ],
            )
            if not tool_calls:
                # 模型给出了正文，本次回复完成
                text_out = (message.get("content") or "").strip()
                debuglog.log(
                    "ai",
                    "final",
                    rounds=round_index + 1,
                    ms=round((time.monotonic() - started) * 1000),
                    chars=len(text_out),
                    rolls=len(rolls),
                    notes=len(notes),
                )
                return text_out, rolls, notes
            if last_round:
                break
            # 把带工具调用的助手消息放回历史，再逐个执行工具
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                name = call.get("function", {}).get("name", "")
                try:
                    args = json.loads(call.get("function", {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    outcome = actions_mod.perform(room, name, args)
                    content = outcome.get("text") or "（无输出）"
                    if outcome.get("attach_board"):
                        content += _board_suffix(room)
                    if outcome.get("roll_record"):
                        rolls.append(outcome["roll_record"])
                    for note_text in outcome.get("notes", []):
                        notes.append({"kind": "system", "who": "系统", "text": note_text})
                except (ValueError, TypeError) as exc:
                    content = f"操作失败：{exc}"
                # 工具结果以 role=tool 回喂给模型
                debuglog.log("ai", "tool", name=name, args=args, result=content)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", "call"),
                        "content": content,
                    }
                )
    debuglog.log("ai", "final", rounds=max_rounds, ms=round((time.monotonic() - started) * 1000), fallback=True)
    return "（AI 这轮没有给出正文回复，请再说一句或重试）", rolls, notes


async def maybe_update_summary(settings, room):
    """
    滚动剧情摘要：每积累 20 条以上日志触发一次，让模型把旧摘要与新剧情
    合并成不超过 500 字的段落（长战役的"记忆"机制）。
    返回 True 表示本次确实做了摘要；失败由调用方静默忽略。
    """
    pending = len(room.log) - room.last_summary_len
    if pending < 20:
        return False
    recent = room.log[-min(pending, 60):]          # 单次最多喂 60 条
    text = "\n".join(
        f"{entry['who']}：{entry['text']}"
        for entry in recent
        if entry.get("kind") in ("chat", "narrate", "roll")
    )
    prompt = (
        f"已有摘要：\n{room.summary or '（无）'}\n\n"
        f"新增剧情：\n{text[:6000]}\n\n"
        "请把两者合并为一段更完整的中文剧情摘要（不超过 500 字），"
        "只保留对后续剧情重要的信息：关键事件、NPC、承诺、线索、地点、人物关系。"
        "直接输出摘要正文，不要任何额外说明。"
    )
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            _endpoint(settings),
            headers=_headers(settings),
            json={"model": _model(settings), "messages": [{"role": "user", "content": prompt}]},
        )
        response.raise_for_status()
        room.summary = response.json()["choices"][0]["message"]["content"].strip()
        room.last_summary_len = len(room.log)
        room.persist_room_meta()
    return True


async def test_connection(settings):
    """
    管理页的"测试连接"：发一条极短请求验证端点/鉴权/模型名。
    思考型模型可能只返回 reasoning_content，这里做兜底文案。
    """
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            _endpoint(settings),
            headers=_headers(settings),
            json={
                "model": _model(settings),
                "messages": [{"role": "user", "content": "只回复四个字：连接成功"}],
                "max_tokens": 256,
            },
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        if not content and message.get("reasoning_content"):
            content = "（连接成功；该模型仅返回思考内容，属思考型模型，不影响跑团）"
        if not content:
            content = "（连接成功，但模型未返回正文）"
        return content


# ---------------------------------------------------------------------------
# 战役结构草案生成（管理页"用 AI 生成草案"按钮调用；非对话流程）
# ---------------------------------------------------------------------------

CAMPAIGN_GEN_PROMPT = """请为一场《黑旗 SRD》（Black Flag Roleplaying）风格的中文短战役设计"结构草案"（等级 1 → 5），只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。

JSON 结构：
{
  "title": "战役名",
  "theme": "主题一句话",
  "pressure_type": "threat_clock",
  "pressure": {"label": "压力名（如：冰龙的侵蚀）", "max": 10, "level": 0},
  "hub": {"name": "枢纽小镇名", "desc": "一句话风情", "giver": "任务发布人（镇长/村长之类）"},
  "quests": [
    {
      "id": "q1", "tier": 1, "title": "任务名", "location": "地点", "giver": "发布人",
      "objective": "明确目标", "hook": "贴在任务板上的一行情报",
      "secret": "仅 GM 知道的转折或真相",
      "encounters": "遭遇设计：怪物类型/数量与 CR 强度",
      "reward": "奖励", "completion": "完成条件", "fail": "失败后果"
    }
  ],
  "finale": {"location": "终局地点", "objective": "最终目标", "condition": "开启条件", "endings": ["结局倾向一", "结局倾向二", "结局倾向三"]}
}

设计约束：
- 共 9 个任务：一阶 3 个（约 1 级）、二阶 3 个（约 3 级）、三阶 3 个（约 5 级）
- 默认主题：冰原白龙威胁村镇（细节请原创，不要照搬任何已出版模组的文本）
- 每个任务是"地点式"单元：一个地点、一个明确目标、2-4 个可玩遭遇；低阶遭遇强度按 1 级队伍控制
- hook 是一行能贴在任务板上的引子；secret 必须是给 GM 的真相/转折，不能是玩家已知信息
- 三阶任务与终局要有递进关系：前两阶揭示线索，第三阶指向终局地点
- pressure_type 固定为 "threat_clock"（进度条）；max 必须大于任务数（9 个任务 → 10，留一步余量）"""


async def generate_campaign_structure(settings):
    """
    让模型直接产出一份战役结构（纯 JSON），解析并校验后返回 dict。
    任何失败抛 ValueError，错误文本会显示在管理页。
    """
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            _endpoint(settings),
            headers=_headers(settings),
            json={
                "model": _model(settings),
                "messages": [{"role": "user", "content": CAMPAIGN_GEN_PROMPT}],
            },
        )
        response.raise_for_status()
        text = (response.json()["choices"][0]["message"].get("content") or "").strip()
    debuglog.log("ai", "campaign_draft", chars=len(text))
    if not text:
        raise ValueError("模型没有输出正文（思考型模型可能占满输出预算），请重试；若持续如此请在管理页更换模型")
    # 容错：去掉可能出现的 markdown 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是有效 JSON（{exc}）")
    errors = campaign_mod.validate_structure(data)
    if errors:
        raise ValueError("草案未通过校验：" + "；".join(errors[:6]))
    return campaign_mod.normalize_structure(data)
