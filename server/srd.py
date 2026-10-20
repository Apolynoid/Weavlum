"""
SRD 规则数据加载层。

职责：把 data/srd/*.json 里的规则数据读进内存并提供查询入口。
数据文件不是代码，改内容不用改这里；本模块不参与任何数值计算
（数值计算分别在 engine / characters / state）。

注意：lru_cache 表示数据在进程生命周期内只读一次——
**修改 JSON 后需要重启服务器才会生效**（有意为之：保证对局中途数据稳定）。
"""

import functools
import json
from pathlib import Path

# 数据目录固定在仓库根目录的 data/srd/ 下
SRD_DIR = Path(__file__).resolve().parent.parent / "data" / "srd"


@functools.lru_cache(maxsize=None)
def _load(filename):
    """读取并缓存一个 JSON 数据文件（UTF-8）。"""
    return json.loads((SRD_DIR / filename).read_text(encoding="utf-8"))


# 以下函数是各数据文件的访问入口；返回值都是 {id: 条目} 的字典
def races():
    return _load("races.json")


def classes():
    return _load("classes.json")


def backgrounds():
    return _load("backgrounds.json")


def skills():
    return _load("skills.json")


def monsters():
    return _load("monsters.json")


def spells():
    return _load("spells.json")


def spell_rules():
    return _load("spell_rules.json")


def items():
    """物品库。BFRD 数据以 name 存中文名，此处补 name_zh 别名以兼容既有调用方。"""
    db = _load("items.json")
    for entry in db.values():
        if "name_zh" not in entry:
            entry["name_zh"] = entry.get("name", "")
    return db


def item_id_by_name(name):
    """按中文名反查物品库 id（建卡出身装备/自由输入对库用）；找不到返回 None。"""
    key = str(name or "").strip()
    if not key:
        return None
    for idx, entry in items().items():
        if entry.get("name_zh") == key:
            return idx
    return None


def race(race_id):
    """按 id 取单个种族（BFRD 语境即"血统"）；找不到时抛中文 ValueError（会直接展示给玩家）。"""
    data = races()
    if race_id not in data:
        raise ValueError(f"未知血统：{race_id}")
    return data[race_id]


def heritages():
    return _load("heritages.json")


def gods():
    """神祇与神系（埃及/希腊/北欧）：神系导语 + 神祇（称号/领域/圣徽）。"""
    return _load("gods.json")


def heritage(heritage_id):
    """按 id 取单个传承；找不到时抛中文 ValueError。"""
    data = heritages()
    if heritage_id not in data:
        raise ValueError(f"未知传承：{heritage_id}")
    return data[heritage_id]


def talents():
    return _load("talents.json")


def chargen():
    """建卡相关规则表（点买花费/总点数/标准数组）。"""
    return _load("chargen.json")


def conditions():
    return _load("conditions.json")


def klass(class_id):
    data = classes()
    if class_id not in data:
        raise ValueError(f"未知职业：{class_id}")
    return data[class_id]


def background(background_id):
    data = backgrounds()
    if background_id not in data:
        raise ValueError(f"未知背景：{background_id}")
    return data[background_id]


def wizard_options():
    """建卡向导页所需的全部选项数据（一次性打包，由路由嵌入页面）。"""
    return {
        "races": races(),
        "heritages": heritages(),
        "classes": classes(),
        "backgrounds": backgrounds(),
        "skills": skills(),
        "talents": talents(),
        "chargen": chargen(),
    }
