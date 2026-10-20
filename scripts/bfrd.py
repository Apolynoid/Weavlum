"""
BFRD（Black Flag SRD v0.2）中文译稿（zh-CN）解析库。

数据源：markdown/Black Flag SRD/zh-CN/（外部数据，不入 git；英文原名以「（English）」括注保留）
本模块提供：文本清洗（含与英文版相同的行内标题伪影处理）、表格解析、实体切分、
括注剥离（展示文本去「（English）」）、术语映射表，供 build_bfrd.py 使用。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZH_DIR = ROOT / "markdown" / "Black Flag SRD" / "zh-CN"

FILES = {
    "license": "BFRD 00 许可与目录.md",
    "creation": "BFRD 01 角色创建与升级.md",
    "classes": "BFRD 02 角色职业.md",
    "lineage": "BFRD 03 血统与传承.md",
    "backgrounds": "BFRD 04 背景与天赋.md",
    "gods": "BFRD 05 神祇与神系.md",
    "equipment": "BFRD 06 装备与魔法物品.md",
    "playing": "BFRD 07 游戏进行.md",
    "spellcasting": "BFRD 08 施法.md",
    "monsters": "BFRD 09 怪物.md",
    "conditions": "BFRD 附录 01 状态.md",
    "glossary": "术语表.md",
}


def clean_text(text):
    """统一换行 + 修复标题伪影（与英文版同源，兼容中文标题）。"""
    text = text.replace("\r\n", "\n")
    # `#- ` 伪影 → 列表项
    text = re.sub(r"(?m)^(\s*)#-\s", r"\1- ", text)
    # "。# ### 标题" 之类的双井号伪影 → 拆出标题
    text = re.sub(r"[ \t]+#{1,6}[ \t]+(?=#{2,6}[ \t])", "\n", text)
    # 行内标题拆行：非空白 + 空白 + ##..###### + 标题首字符（中文/英文/数字）
    for _ in range(3):
        text = re.sub(r"(\S)[ \t]+(#{2,6}[ \t]+[^\s#])", r"\1\n\2", text)
    # 行内散装 #word（如 "的 #每次攻击"）→ 去井号（不碰行首标题）
    text = re.sub(r"(\S)[ \t]#(?=[\u4e00-\u9fffA-Za-z])", r"\1 ", text)
    return text


def load(key):
    """读取并预处理一个文档，返回行列表。"""
    return clean_text((ZH_DIR / FILES[key]).read_text(encoding="utf-8")).split("\n")


def load_full(key):
    return clean_text((ZH_DIR / FILES[key]).read_text(encoding="utf-8"))


def split_sections(lines, pattern):
    """按标题正则切块：返回 [(标题, 正文行列表), ...]。"""
    rx = re.compile(pattern)
    sections = []
    current = None
    for line in lines:
        if rx.match(line.strip()):
            if current:
                sections.append(current)
            current = (line.strip(), [])
        elif current is not None:
            current[1].append(line)
    if current:
        sections.append(current)
    return sections


def parse_table(lines, start_index):
    """从 start_index 解析一个 markdown 管道表，返回 (rows, next_index)。rows 含表头、去分隔行。"""
    def cells(line):
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            return None
        return [c.strip() for c in line.strip("|").split("|")]

    if start_index >= len(lines):
        return [], start_index
    header = cells(lines[start_index])
    if header is None:
        return [], start_index
    index = start_index + 1
    if index < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[index]):
        index += 1
    rows = [header]
    while index < len(lines):
        row = cells(lines[index])
        if row is None:
            break
        rows.append(row)
        index += 1
    return rows, index


def find_tables(lines):
    """扫描全文，返回 [(起行号, rows)]（每张表含表头行）。"""
    tables = []
    index = 0
    while index < len(lines):
        rows, next_index = parse_table(lines, index)
        if rows:
            tables.append((index, rows))
            index = next_index
        else:
            index += 1
    return tables


# ---------------------------------------------------------------------------
# 括注处理：中文（English）→ 拆分 / 剥离
# ---------------------------------------------------------------------------

# 仅当括号内含「连续两个以上拉丁字母」的英文词时视为术语括注（保留骰子如 3d6、缩写数字类）
_ASCII_ANNOT = re.compile(r"（(?=[^（）]*[A-Za-z]{2})[A-Za-z0-9'’\-\.,/& ()]*）")


def strip_annotations(text):
    """剥离「（English）」术语括注（含嵌套），得到干净中文展示文本。"""
    text = str(text or "")
    for _ in range(6):
        new = _ASCII_ANNOT.sub("", text)
        if new == text:
            break
        text = new
    text = text.replace("（）", "")
    text = re.sub(r"\s+([，。；、）])", r"\1", text)
    text = re.sub(r"\s+([为是的中了与和及。])", r"\1", text)
    text = re.sub(r"([（])\s+", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


_HEAD_ANNOT = re.compile(r"^(.+?)（(.+?)）$")


def split_annot(text):
    """标题「中文（English）」→ (中文, English)；无括注时 English 为空串。"""
    cleaned = str(text).strip().lstrip("-•* ").strip().rstrip("：")
    match = _HEAD_ANNOT.match(cleaned)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return cleaned, ""


def inner_zh(text):
    """去掉全部括注后取中文主体（用于表格单元格等）。"""
    return strip_annotations(text)


def slugify(name):
    """英文名 → id。"""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


_ZH_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def zh_to_int(token):
    """中文数字（一~十）或阿拉伯数字 → int；无法识别时返回 None。"""
    token = str(token).strip()
    if token.isdigit():
        return int(token)
    return _ZH_NUM.get(token)


# ---------------------------------------------------------------------------
# 常用映射表（与译稿术语一致）
# ---------------------------------------------------------------------------

SKILL_ZH_ID = {
    "杂技": "acrobatics", "驯兽": "animal_handling", "奥秘": "arcana", "运动": "athletics",
    "欺瞒": "deception", "历史": "history", "洞悉": "insight", "威吓": "intimidation",
    "调查": "investigation", "医药": "medicine", "自然": "nature", "察觉": "perception",
    "表演": "performance", "游说": "persuasion", "宗教": "religion", "巧手": "sleight_of_hand",
    "隐匿": "stealth", "求生": "survival",
}

# 英文技能名 → id（优先用英文侧映射，避免中文名带前缀词）
SKILL_EN_ID = {
    "acrobatics": "acrobatics", "animal handling": "animal_handling", "arcana": "arcana",
    "athletics": "athletics", "deception": "deception", "history": "history", "insight": "insight",
    "intimidation": "intimidation", "investigation": "investigation", "medicine": "medicine",
    "nature": "nature", "perception": "perception", "performance": "performance",
    "persuasion": "persuasion", "religion": "religion", "sleight of hand": "sleight_of_hand",
    "stealth": "stealth", "survival": "survival",
}


def skill_id(en_name, zh_name=""):
    """技能（英文优先，中文兜底）→ id。"""
    key = str(en_name or "").strip().lower()
    if key in SKILL_EN_ID:
        return SKILL_EN_ID[key]
    zh = re.sub(r"^[从或和\s]+", "", str(zh_name or "").strip())
    return SKILL_ZH_ID.get(zh)

ABILITY_ZH_ID = {"力量": "str", "敏捷": "dex", "体质": "con", "智力": "int", "感知": "wis", "魅力": "cha"}
ABILITY_ABBR_ID = {"STR": "str", "DEX": "dex", "CON": "con", "INT": "int", "WIS": "wis", "CHA": "cha"}

SOURCE_ZH_ID = {"奥术": "arcane", "神术": "divine", "原初": "primordial", "命运": "wyrd"}
SOURCE_EN_ID = {"arcane": "arcane", "divine": "divine", "primordial": "primordial", "wyrd": "wyrd"}

DAMAGE_ZH = {"强酸", "钝击", "寒冷", "火焰", "力场", "闪电", "死灵", "穿刺", "毒素", "心灵", "光耀", "斩击", "雷鸣"}

RARITY_ZH = {"普通": "普通", "罕见": "罕见", "稀有": "稀有", "非常稀有": "非常稀有", "传说": "传说"}

CN_NUMS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn_number(text):
    """中文/阿拉伯数字 → int；解析失败返回 None。"""
    match = re.search(r"(\d+)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"([一二两三四五六七八九十])", text)
    if match:
        return CN_NUMS.get(match.group(1))
    return None
