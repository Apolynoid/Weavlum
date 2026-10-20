import random
import unittest

from server import spells, srd


def make_wizard(level=1, mode="class", picks=None):
    """构造一个最小可用的法师角色（字段手工拼接，不依赖建卡向导全流程）。"""
    char = {
        "name": "测试法师",
        "class_id": "wizard",
        "level": level,
        "hit_die": 6,
        "prof_bonus": spells.PROF_BY_LEVEL[level],
        "mods": {"str": -1, "dex": 2, "con": 1, "int": 4, "wis": 0, "cha": 0},
        "saves": {"str": -1, "dex": 2, "con": 1, "int": 6, "wis": 2, "cha": 0},
        "skills": {"arcana": 6},
        "hp": {"max": 8, "cur": 8},
        "spellcasting": {"ability": "int", "save_dc": 14, "attack_bonus": 6},
    }
    if picks is None:
        picks = ["fire-bolt", "magic-missile", "shield"]
    char.update(spells.build_spell_state(picks, "wizard", level, mode))
    return char


class DataTests(unittest.TestCase):
    def test_data_integrity(self):
        for sid, entry in srd.spells().items():
            self.assertLessEqual(entry["level"], 9, sid)    # v0.4.0：0-9 环全量
            for field in ("name", "name_en", "school", "casting_time", "range", "duration", "classes"):
                self.assertIn(field, entry, f"{sid} 缺字段 {field}")

    def test_recommended_ids_exist_and_match_class(self):
        data = srd.spells()
        for klass_id, picks in (srd.spell_rules().get("recommended") or {}).items():
            for sid in picks:
                self.assertIn(sid, data, f"推荐包里 {sid} 不存在")
                self.assertIn(klass_id, data[sid]["classes"], f"{sid} 不属于职业 {klass_id}")

    def test_rules_tables_cover_casters(self):
        rules = srd.spell_rules()
        for klass_id in rules["caster_classes"]:
            self.assertIn(klass_id, rules["slots"])
            self.assertIn(klass_id, rules["counts"])
            for level in ("1", "2", "3", "4", "5", "6"):
                self.assertIn(level, rules["slots"][klass_id])


class LimitTests(unittest.TestCase):
    def _subset(self, got, expected):
        for key, value in expected.items():
            self.assertEqual(got[key], value, key)

    def test_limits(self):
        self._subset(spells.limits_for("wizard", 1), {"cantrips": 3, "spells": 2, "prepared": 2, "rituals": 1, "max_spell_level": 1})
        self.assertEqual(spells.limits_for("wizard", 3)["max_spell_level"], 2)
        self._subset(spells.limits_for("paladin", 1), {"cantrips": 0, "spells": 0, "max_spell_level": 0})
        self._subset(spells.limits_for("paladin", 2), {"cantrips": 0, "spells": 2, "max_spell_level": 1})
        self.assertEqual(spells.limits_for("warlock", 5)["max_spell_level"], 2)
        self.assertEqual(spells.limits_for("wizard", 6)["max_spell_level"], 3)
        self._subset(spells.limits_for("warlock", 11), {"cantrips": 4, "spells": 7, "max_spell_level": 3})
        # 准备施法者动态上限：法师 = 智力调整值 + 等级
        char = {"mods": {"int": 4}}
        self.assertEqual(spells.limits_for("wizard", 5, char)["spells"], 9)
        self.assertEqual(spells.limits_for("wizard", 20, {"mods": {"int": 5}})["spells"], 25)
        with self.assertRaises(ValueError):
            spells.limits_for("fighter", 1)

    def test_validate_picks(self):
        self.assertEqual(spells.validate_spell_picks(["fire-bolt", "magic-missile"], "wizard", 1, "class"), [])
        errors = spells.validate_spell_picks(["bless"], "wizard", 1, "class")
        self.assertTrue(any("无法学习" in e for e in errors))
        self.assertEqual(spells.validate_spell_picks(["bless"], "wizard", 1, "free"), [])
        errors = spells.validate_spell_picks(["fireball"], "wizard", 1, "free")
        self.assertTrue(any("环阶" in e for e in errors))
        errors = spells.validate_spell_picks(["fire-bolt", "fire-bolt"], "wizard", 1, "class")
        self.assertTrue(any("重复" in e for e in errors))
        errors = spells.validate_spell_picks(
            ["fire-bolt", "ray-of-frost", "prestidigitation", "light"], "wizard", 1, "free"
        )
        self.assertTrue(any("戏法最多" in e for e in errors))

    def test_build_spell_state(self):
        char = make_wizard()
        self.assertEqual(char["spell_slots"], {"1": {"max": 2, "used": 0}})
        self.assertEqual(len(char["spells"]), 3)
        self.assertIsNone(char["concentration"])


class DamageDiceTests(unittest.TestCase):
    def test_dice_selection(self):
        fireball = spells.spell("fireball")
        self.assertEqual(spells.damage_dice(fireball["damage"], 5, 5, 3), "10d6")
        self.assertEqual(spells.damage_dice(fireball["damage"], 5, 3, 3), "8d6")
        fire_bolt = spells.spell("fire-bolt")
        self.assertEqual(spells.damage_dice(fire_bolt["damage"], 5, 0, 0), "2d10")
        magic_missile = spells.spell("magic-missile")
        self.assertEqual(spells.damage_dice(magic_missile["damage"], 1, 2, 1), "4d4 + 4")


class CastTests(unittest.TestCase):
    def test_cast_cantrip_no_slot(self):
        char = make_wizard()
        summary, meta = spells.cast_spell(char, "fire-bolt")
        self.assertIn("戏法", summary)
        self.assertEqual(char["spell_slots"]["1"]["used"], 0)
        self.assertTrue(meta["rolls"])

    def test_cast_expends_slots_and_blocks(self):
        char = make_wizard()
        spells.cast_spell(char, "magic-missile")
        self.assertEqual(char["spell_slots"]["1"]["used"], 1)
        spells.cast_spell(char, "magic-missile")
        self.assertEqual(char["spell_slots"]["1"]["used"], 2)
        with self.assertRaises(ValueError):
            spells.cast_spell(char, "magic-missile")    # 2/2 用尽

    def test_cast_unknown_or_unlearned(self):
        char = make_wizard()
        with self.assertRaises(ValueError):
            spells.cast_spell(char, "cure-wounds")      # 未习得
        with self.assertRaises(ValueError):
            spells.cast_spell(char, "not-a-real-spell")  # 未知 id

    def test_concentration_replaces_previous(self):
        char = make_wizard(picks=["fire-bolt", "bless", "shield-of-faith"], mode="free")
        summary, _ = spells.cast_spell(char, "bless")
        self.assertEqual(char["concentration"], "祝福术")
        self.assertEqual(char["spell_slots"]["1"]["used"], 1)
        summary2, _ = spells.cast_spell(char, "shield-of-faith")
        self.assertEqual(char["concentration"], "信仰护盾")
        names = [effect["name"] for effect in char["effects"]]
        self.assertNotIn("祝福术", names)
        self.assertIn("结束", summary2)

    def test_effects_expire_by_clock(self):
        char = make_wizard(picks=["fire-bolt", "bless"], mode="free")
        spells.cast_spell(char, "bless")    # 1 分钟（10 轮）
        self.assertIsNotNone(char["effects"][0]["expires_at"])
        self.assertEqual(spells.expire_by_clock(char, 0.5), [])
        expired = spells.expire_by_clock(char, 1.0)
        self.assertTrue(any("祝福术" in text for text in expired))
        self.assertIsNone(char["concentration"])

    def test_concentration_save(self):
        char = make_wizard(picks=["fire-bolt", "bless"], mode="free")
        spells.cast_spell(char, "bless")
        rng = random.Random(3)
        mirror = random.Random(3)
        first_roll = mirror.randint(1, 20)
        bonus = char["saves"]["con"]
        text = spells.concentration_save(char, 12, rng=rng)    # DC = max(10, 6) = 10
        if first_roll + bonus >= 10:
            self.assertIn("保持", text)
            self.assertEqual(char["concentration"], "祝福术")
        else:
            self.assertIn("中断", text)
            self.assertIsNone(char["concentration"])

    def test_no_concentration_no_save(self):
        char = make_wizard()
        self.assertIsNone(spells.concentration_save(char, 10))


class LearnLevelTests(unittest.TestCase):
    def test_level_up(self):
        char = make_wizard(level=1)
        old_hp = char["hp"]["max"]
        spells.level_up(char)
        self.assertEqual(char["level"], 2)
        self.assertEqual(char["prof_bonus"], 2)
        self.assertGreater(char["hp"]["max"], old_hp)
        self.assertEqual(char["spell_slots"]["1"]["max"], 3)
        char["level"] = 4
        spells.level_up(char)
        self.assertEqual(char["prof_bonus"], 3)
        self.assertEqual(char["spellcasting"]["save_dc"], 8 + 3 + 4)
        self.assertEqual(sorted(char["spell_slots"].keys()), ["1", "2", "3"])
        char["level"] = 5
        spells.level_up(char)
        self.assertEqual(char["level"], 6)
        self.assertEqual(char["spell_slots"]["3"]["max"], 3)
        while int(char["level"]) < spells.MAX_LEVEL:
            spells.level_up(char)    # 一路升到上限
        self.assertEqual(char["level"], 20)
        with self.assertRaises(ValueError):
            spells.level_up(char)    # 20 级为上限

    def test_learn_and_forget(self):
        char = make_wizard(level=1)
        sid, entry = spells.find_spell("burning-hands")
        text = spells.learn_spell(char, sid, entry)
        self.assertIn("学会", text)
        with self.assertRaises(ValueError):
            spells.learn_spell(char, sid, entry)                          # 重复
        with self.assertRaises(ValueError):
            spells.learn_spell(char, *spells.find_spell("fireball"))      # 环阶过高
        with self.assertRaises(ValueError):
            spells.learn_spell(char, *spells.find_spell("bless"))         # 职业不符
        text = spells.forget_spell(char, "burning-hands")
        self.assertIn("忘却", text)

    def test_effect_tools(self):
        char = make_wizard()
        text = spells.add_effect(char, "闪避", 1, note="闪避动作")
        self.assertIn("闪避", text)
        text = spells.remove_effect(char, "闪避")
        self.assertIn("已移除", text)
        with self.assertRaises(ValueError):
            spells.remove_effect(char, "不存在")


if __name__ == "__main__":
    unittest.main()
