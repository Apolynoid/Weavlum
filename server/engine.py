"""
掷骰与判定的唯一计算引擎。

职责边界：本模块只做纯数学计算——不接触网络、不读写文件、不感知房间状态。
所有随机数（玩家掷骰、AI 工具掷骰、先攻、建卡骰点）最终都收敛到这里，
这是整个平台"数值权威"的源头。

设计约定：所有函数都支持注入 rng（random.Random 实例），
方便单元测试用固定种子复现骰点序列。
"""

import random
import re

# 掷骰表达式的词法规则：一个 token 要么是 NdM 形式的骰子，要么是普通整数；
# 除第一个 token 外都必须带 +/- 符号。例如 "1d20+5"、"2d6-1"、"4d6"。
TOKEN_RE = re.compile(r"[+-]?(?:\d*[dD]\d+|\d+)")


def _pick(rng):
    """统一取随机源：显式传入的 rng 优先（测试用），否则用全局 random。"""
    return rng if rng is not None else random


def _format_detail(detail):
    """把解析明细拼成人类可读的展开式，例如：2d6(3,5) + 1d4(2) - 1。"""
    parts = []
    for item in detail:
        if item["kind"] == "dice":
            body = f"{item['count']}d{item['faces']}({','.join(map(str, item['rolls']))})"
        else:
            body = str(item["value"])
        if not parts:
            # 第一段：负号直接贴前面，正号省略
            parts.append(("- " if item["sign"] < 0 else "") + body)
        else:
            parts.append((" - " if item["sign"] < 0 else " + ") + body)
    return "".join(parts)


def roll_expr(expr, rng=None):
    """
    解析并执行一个掷骰表达式（如 "1d20+5"、"3d1"、"-1d1+10"）。

    返回统一结构：
      expr      规范化后的表达式
      total     最终总值
      detail    每个 token 的明细（几个骰子、各自点数、符号）
      breakdown 供日志展示的展开字符串
    解析失败或取值超范围时抛 ValueError（由调用方转成中文提示）。
    """
    r = _pick(rng)
    # 归一化：去空格、转小写，统一处理 "2D6 + 3" 这类输入
    s = (expr or "").strip().replace(" ", "").lower()
    if not s:
        raise ValueError("掷骰表达式为空")
    tokens = TOKEN_RE.findall(s)
    # 词法校验：所有 token 拼起来必须与原串完全一致，
    # 否则说明混入了非法字符（如 "2d6*2"、"1d20++5"）
    if not tokens or "".join(tokens) != s:
        raise ValueError(f"无法解析表达式：{expr}")
    total = 0
    detail = []
    for tok in tokens:
        sign = -1 if tok.startswith("-") else 1
        body = tok.lstrip("+-")
        if "d" in body:
            # 骰子 token：拆成数量与面数，"d20" 视为 "1d20"
            count_s, _, faces_s = body.partition("d")
            count = int(count_s) if count_s else 1
            faces = int(faces_s)
            if not 1 <= count <= 100:
                raise ValueError("骰子数量需在 1-100 之间")
            if not 1 <= faces <= 1000:
                raise ValueError("骰子面数需在 1-1000 之间")
            rolls = [r.randint(1, faces) for _ in range(count)]
            total += sign * sum(rolls)
            detail.append({"kind": "dice", "count": count, "faces": faces, "rolls": rolls, "sign": sign})
        else:
            # 纯整数修正值，如 "+5"
            value = int(body)
            total += sign * value
            detail.append({"kind": "mod", "value": value, "sign": sign})
    return {"expr": s, "total": total, "detail": detail, "breakdown": _format_detail(detail)}


def roll_check(modifier=0, dc=None, mode="normal", label="", rng=None):
    """
    单 d20 检定：普通 / 优势（掷两次取高）/ 劣势（取低）。

    - modifier：角色对应的加值（技能、豁免、攻击等，来自 party_brief 的实时数值）
    - dc：可选；传入时直接判定成功/失败，不传则只报数
    - 返回结构含 nat20/nat1（天然大成功/大失败）与 breakdown（如 "d20(13) + 5 = 18"）
    """
    r = _pick(rng)
    if mode not in ("normal", "advantage", "disadvantage"):
        mode = "normal"
    rolls = [r.randint(1, 20)]
    if mode != "normal":
        rolls.append(r.randint(1, 20))
        taken = max(rolls) if mode == "advantage" else min(rolls)
    else:
        taken = rolls[0]
    total = taken + modifier
    # 展示格式：优势/劣势时把两次原始点数都带上，方便人工核验规则是否正确
    if len(rolls) > 1:
        rolls_txt = f"d20[{','.join(map(str, rolls))}→{taken}]"
    else:
        rolls_txt = f"d20({taken})"
    mod_txt = f" {modifier:+d}" if modifier else ""
    return {
        "kind": "check",
        "label": label,
        "mode": mode,
        "rolls": rolls,
        "taken": taken,
        "modifier": modifier,
        "total": total,
        "dc": dc,
        "nat20": taken == 20,
        "nat1": taken == 1,
        "success": (total >= dc) if dc is not None else None,
        "breakdown": f"{rolls_txt}{mod_txt} = {total}",
    }


def roll_ability_scores(rng=None):
    """建卡用的 4d6 取三高：掷 6 组，每组返回原始点数与被保留的三个点数。"""
    r = _pick(rng)
    results = []
    for _ in range(6):
        rolls = [r.randint(1, 6) for _ in range(4)]
        kept = sorted(rolls, reverse=True)[:3]
        results.append({"rolls": rolls, "kept": kept, "total": sum(kept)})
    return results
