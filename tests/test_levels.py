"""
1-20 级系统测试：熟练表、法术位表（含 4-9 环/契约位/半施法）、等级上限、
属性提升（pending/应用/上限 20/派生重算/同伴自动）。
"""

import tempfile
import unittest
from pathlib import Path

from server import actions, characters, spells, srd
from server import state as state_mod
from tests.test_combat import make_room


class LevelSystemTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_char_dir = state_mod.CHAR_DIR
        self._orig_meta_file = state_mod.ROOM_META_FILE
        state_mod.CHAR_DIR = Path(self._tmp.name) / "characters"
        state_mod.ROOM_META_FILE = Path(self._tmp.name) / "room_meta.json"

    def tearDown(self):
        state_mod.CHAR_DIR = self._orig_char_dir
        state_mod.ROOM_META_FILE = self._orig_meta_file
        self._tmp.cleanup()

    def test_prof_table_1_to_20(self):
        self.assertEqual(spells.MAX_LEVEL, 20)
        self.assertEqual(spells.PROF_BY_LEVEL[1], 2)
        self.assertEqual(spells.PROF_BY_LEVEL[5], 3)
        self.assertEqual(spells.PROF_BY_LEVEL[9], 4)
        self.assertEqual(spells.PROF_BY_LEVEL[13], 5)
        self.assertEqual(spells.PROF_BY_LEVEL[17], 6)
        self.assertEqual(spells.PROF_BY_LEVEL[20], 6)

    def test_slots_official_values(self):
        """BFRD 抽查：法师 9 级、邪术师 11 级（常规法术位）、圣武士 5 级、游侠 1 级无位。"""
        self.assertEqual(spells.slots_for("wizard", 9), [4, 3, 3, 3, 1])
        self.assertEqual(spells.slots_for("warlock", 11), [4, 3, 3])
        self.assertEqual(spells.slots_for("paladin", 5), [4, 2])
        self.assertEqual(spells.slots_for("paladin", 1), [])
        limits = spells.limits_for("wizard", 20, {"mods": {"int": 5}})
        self.assertEqual(limits["max_spell_level"], 9)
        self.assertEqual(limits["spells"], 25)          # 准备型：智力调整值 + 等级

    def test_spells_data_extended(self):
        db = srd.spells()
        self.assertGreater(len(db), 300)
        self.assertTrue(any(entry.get("level") == 9 for entry in db.values()))

    def test_level_up_to_20_and_cap(self):
        room, char = make_room()
        while int(char["level"]) < 20:
            spells.level_up(char)
        self.assertEqual(char["level"], 20)
        self.assertEqual(char["prof_bonus"], 6)
        with self.assertRaises(ValueError):
            spells.level_up(char)

    def test_asi_pending_accrues_at_asi_levels(self):
        room, char = make_room()
        for _ in range(3):
            spells.level_up(char)          # 2/3/4 级
        self.assertEqual(char["level"], 4)
        self.assertEqual(char["pending_asi"], 1)
        for _ in range(4):
            spells.level_up(char)          # 5-8 级
        self.assertEqual(char["pending_asi"], 3)   # 4、6（战士额外档）、8 级各一次

    def test_apply_asi_validation(self):
        room, char = make_room()
        char["pending_asi"] = 1
        with self.assertRaises(ValueError):
            characters.apply_asi(char, {"str": 1})            # 点数不足
        with self.assertRaises(ValueError):
            characters.apply_asi(char, {"str": 3})            # 单次不能 +3
        with self.assertRaises(ValueError):
            characters.apply_asi(char, {"luck": 2})           # 未知属性

    def test_apply_asi_effects(self):
        """+2 力量：属性/修正/派生重算 + pending 减少；上限 20 拒绝。"""
        room, char = make_room()
        str_before = int(char["abilities"]["str"])
        char["pending_asi"] = 1
        summary = characters.apply_asi(char, {"str": 2})
        self.assertIn("力量 +2", summary)
        self.assertEqual(char["abilities"]["str"], str_before + 2)
        self.assertEqual(char["mods"]["str"], characters.modifier(str_before + 2))
        self.assertEqual(char["pending_asi"], 0)
        char["pending_asi"] = 1
        char["abilities"]["str"] = 19
        with self.assertRaises(ValueError):
            characters.apply_asi(char, {"str": 2})

    def test_apply_asi_con_retroactive_hp(self):
        """体质提升按既往等级回补 HP（每级每 +1 修正 +1 HP）。"""
        room, char = make_room()
        char["abilities"]["con"] = 12
        characters.recompute_derived(char)
        char["pending_asi"] = 1
        hp_before = char["hp"]["max"]
        characters.apply_asi(char, {"con": 2})               # 12→14，修正 +1
        self.assertEqual(char["hp"]["max"], hp_before + int(char["level"]))

    def test_apply_asi_dex_updates_light_armor_ac(self):
        room, char = make_room()
        char["worn_armor"] = "leather"          # 换穿皮甲（轻甲：敏捷全额计入 AC）
        characters.recompute_derived(char)
        self.assertEqual(char["armor"], "皮甲")
        ac_before = char["ac"]
        char["pending_asi"] = 1
        characters.apply_asi(char, {"dex": 2})
        self.assertEqual(char["ac"], ac_before + 1)

    def test_apply_asi_action_and_tool_shape(self):
        room, char = make_room()
        with self.assertRaises(ValueError):
            actions.perform(room, "apply_asi", {"character": "测试勇者", "asi": {"str": 2}})
        char["pending_asi"] = 1
        out = actions.perform(room, "apply_asi", {"character": "测试勇者", "asi": {"str": 2}})
        self.assertTrue(out["changed"])
        self.assertIn("力量 +2", out["text"])
        # AI 工具参数形态（plus_two / plus_one）
        char["pending_asi"] = 1
        out = actions.perform(room, "apply_asi", {"character": "测试勇者", "plus_two": "dex"})
        self.assertIn("敏捷 +2", out["text"])
        char["pending_asi"] = 1
        out = actions.perform(room, "apply_asi", {"character": "测试勇者", "plus_one": ["int", "wis"]})
        self.assertIn("智力 +1", out["text"])
        self.assertIn("感知 +1", out["text"])

    def test_companion_auto_asi(self):
        """同伴无玩家：待分配 ASI 按主属性自动结算（战士主属性力量）。"""
        room, char = make_room()
        char["pending_asi"] = 2
        text = characters.settle_companion_asi(char)
        self.assertIn("力量 +2", text)
        self.assertEqual(char["pending_asi"], 0)
        self.assertEqual(char["abilities"]["str"], 19)  # BFRD 人类无种族加值：15 → +2 → +2


class ProgressionChoiceTests(unittest.TestCase):
    """BFRD 进度选择：3 级子职业、提升（Improvement）三式、10/20 级恩赐、同伴自动结算。"""

    def test_improvement_levels_per_class(self):
        """提升档位按职业进度表：战士 7 档（含 6/14），野蛮人基准 5 档。"""
        self.assertEqual(characters.improvement_levels("fighter"), [4, 6, 8, 12, 14, 16, 19])
        self.assertEqual(characters.improvement_levels("barbarian"), [4, 8, 12, 16, 19])
        room, char = make_room()
        while int(char["level"]) < 6:
            spells.level_up(char)
        self.assertEqual(char["pending_asi"], 2)      # 4 级与 6 级各一次（未分配则累计）

    def test_subclass_at_level_three(self):
        room, char = make_room()
        with self.assertRaises(ValueError):
            characters.choose_subclass(char, "weapon-master")     # 1 级不能选子职业
        spells.level_up(char)
        self.assertFalse(char.get("pending_subclass"))
        spells.level_up(char)
        self.assertTrue(char.get("pending_subclass"))
        out = characters.choose_subclass(char, "weapon-master")
        self.assertIn("武器大师", out)
        self.assertEqual(char["subclass"], "weapon-master")
        with self.assertRaises(ValueError):
            characters.choose_subclass(char, "hunter")            # 不属于战士
        with self.assertRaises(ValueError):
            characters.choose_subclass(char, "not-a-subclass")

    def test_improvement_talent_mode(self):
        room, char = make_room()
        char["pending_asi"] = 1
        summary = characters.apply_asi(char, {"dex": 1}, talent_id="athletic")
        self.assertIn("健儿", summary)
        self.assertIn("athletic", char["talents"])
        char["pending_asi"] = 1
        with self.assertRaises(ValueError):
            characters.apply_asi(char, {"dex": 1}, talent_id="arcanist")   # 魔法天赋不在战士列表
        char["pending_asi"] = 1
        with self.assertRaises(ValueError):
            characters.apply_asi(char, {"dex": 2}, talent_id="athletic")   # 带天赋只能 +1 一项

    def test_boons_at_levels(self):
        room, char = make_room()
        char["level"] = 9
        spells.level_up(char)
        self.assertIn(10, char["pending_boons"])
        options = characters.boon_options("fighter", 10)
        self.assertGreaterEqual(len(options), 2)
        characters.choose_boon(char, 10, options[0]["key"])
        self.assertEqual(char["pending_boons"], [])
        with self.assertRaises(ValueError):
            characters.choose_boon(char, 10, "0")

    def test_companion_progression_auto(self):
        room, _ = make_room()
        comp = state_mod.create_companion(room, "自动剑", "fighter", "human", 10)
        self.assertTrue(comp.get("subclass"))
        self.assertEqual([b["level"] for b in comp["boons"]], [10])


if __name__ == "__main__":
    unittest.main()
