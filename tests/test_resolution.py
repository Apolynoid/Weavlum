"""
结算系统测试（v0.6.1）：
濒死/死亡豁免/稳定/复活、力竭六档、通用技能检定、仪式施法、同名效果不叠加、
Downtime 五活动、载具受损与修理。
"""

import tempfile
import unittest
from pathlib import Path

from server import actions, characters, spells
from server import state as state_mod
from tests.test_combat import make_room


class LifeStateTests(unittest.TestCase):
    """濒死 → 死亡豁免 → 稳定/死亡 → 复活 全链（引擎自动结算）。"""

    def test_dying_flow(self):
        room, char = make_room()
        out = actions.perform(room, "adjust_hp", {"target": "测试勇者", "delta": -999})
        self.assertTrue(char["dead"])                                  # 巨量伤害（溢出 986 ≥ 13）
        self.assertIn("巨量伤害", out["text"])
        actions.perform(room, "adjust_hp", {"target": "测试勇者", "delta": 5})
        self.assertEqual(char["hp"]["cur"], 0)                         # 死者不受治疗

    def test_nonlethal_drop_and_healing(self):
        room, char = make_room()
        actions.perform(room, "adjust_hp", {"target": "测试勇者", "delta": -13})
        self.assertTrue(char["dying"])
        self.assertEqual([e["name"] for e in char["effects"]][:2], ["昏迷", "倒地"])
        actions.perform(room, "adjust_hp", {"target": "测试勇者", "delta": -3})
        self.assertEqual(char["death_saves"]["failures"], 1)           # 0 HP 再受伤自动失败一次
        actions.perform(room, "adjust_hp", {"target": "测试勇者", "delta": 5})
        self.assertFalse(char["dying"])
        self.assertEqual(char["hp"]["cur"], 5)
        self.assertNotIn("昏迷", [e["name"] for e in char["effects"]])

    def test_death_save_rolls(self):
        room, char = make_room()
        char["dying"] = True
        char["hp"]["cur"] = 0
        texts, events = characters.death_save_roll(char, 10)
        self.assertIn("success", events)
        texts, events = characters.death_save_roll(char, 1)            # 掷 1 记两次失败
        self.assertEqual(char["death_saves"]["failures"], 2)
        texts, events = characters.death_save_roll(char, 5)            # 第三败 → 死亡
        self.assertIn("dead", events)
        self.assertTrue(char["dead"])

    def test_three_successes_stabilize_and_recover(self):
        room, char = make_room()
        char["dying"] = True
        char["hp"]["cur"] = 0
        for _ in range(3):
            characters.death_save_roll(char, 12)
        self.assertTrue(char["stable"])
        self.assertEqual(char["death_saves"], {"successes": 0, "failures": 0})
        char["stable_until"] = room.clock_minutes + 60
        notes = state_mod.advance_clock(room, 61)
        self.assertEqual(char["hp"]["cur"], 1)                         # 稳定 1d4 小时后自动苏醒
        self.assertFalse(char["stable"])
        self.assertTrue(any("苏醒" in n for n in notes))

    def test_natural_20_recovers(self):
        room, char = make_room()
        char["dying"] = True
        char["hp"]["cur"] = 0
        texts, events = characters.death_save_roll(char, 20)
        self.assertIn("recover", events)
        self.assertEqual(char["hp"]["cur"], 1)
        self.assertFalse(char["dying"])

    def test_turn_start_auto_death_save(self):
        room, char = make_room()
        char["dying"] = True
        char["hp"]["cur"] = 0
        room.combat = {
            "active": True, "round": 1, "turn": 0,
            "combatants": [{"id": char["id"], "name": char["name"], "kind": "pc", "init": 10,
                            "hp": 0, "hp_max": char["hp"]["max"], "speed": 30,
                            "pos": {"x": 15.0, "y": 30.0}, "turn_start": {"x": 15.0, "y": 30.0}, "dashing": False}],
        }
        notes = state_mod.next_turn(room)
        self.assertTrue(any("死亡豁免" in n for n in notes))

    def test_revive_action(self):
        room, char = make_room()
        char["class_id"] = "cleric"
        char["spells"] = [{"id": "revivify", "name": "复苏术", "level": 3}]
        char["spell_slots"] = {"3": {"max": 1, "used": 0}}
        char["hp"]["cur"] = 0
        characters.mark_dead(char)
        out = actions.perform(room, "revive", {"caster": "测试勇者", "character": "测试勇者"})
        self.assertFalse(char["dead"])
        self.assertEqual(char["hp"]["cur"], 1)
        self.assertEqual(char["spell_slots"]["3"]["used"], 1)
        with self.assertRaises(ValueError):
            actions.perform(room, "revive", {"caster": "测试勇者", "character": "测试勇者"})

    def test_stabilize_action(self):
        room, char = make_room()
        char["dying"] = True
        char["hp"]["cur"] = 0
        char["skills"]["medicine"] = 2                                # 保证通过率无关紧要：只测流程
        out = actions.perform(room, "stabilize", {"target": "测试勇者", "by": "测试勇者"})
        self.assertIn("稳定", out["text"])


class ExhaustionTests(unittest.TestCase):
    def test_levels_apply(self):
        room, char = make_room()
        characters.apply_exhaustion(char, 2)
        self.assertEqual(char["speed"], 15)                           # 速度减半
        characters.apply_exhaustion(char, 4)
        self.assertEqual(char["hp"]["max"], max(1, char["hp_max_base"] // 2))
        characters.apply_exhaustion(char, 5)
        self.assertEqual(char["speed"], 0)
        characters.apply_exhaustion(char, 0)
        self.assertEqual(char["speed"], char["speed_base"])
        self.assertEqual(char["hp"]["max"], char["hp_max_base"])

    def test_level_six_kills(self):
        room, char = make_room()
        characters.apply_exhaustion(char, 6)
        self.assertTrue(char["dead"])

    def test_skill_check_auto_disadvantage(self):
        room, char = make_room()
        check = characters.skill_check(char, "perception", dc=10)
        self.assertEqual(check["mode"], "normal")
        characters.apply_exhaustion(char, 1)
        check = characters.skill_check(char, "perception", dc=10)
        self.assertEqual(check["mode"], "disadvantage")               # 力竭 1 级：属性检定劣势
        self.assertEqual(check["skill"], "perception")

    def test_long_rest_reduces(self):
        room, char = make_room()
        characters.apply_exhaustion(char, 2)
        notes = state_mod.long_rest(room)
        self.assertEqual(char["exhaustion"], 1)
        self.assertTrue(any("力竭" in n for n in notes))

    def test_action_adjust(self):
        room, char = make_room()
        out = actions.perform(room, "adjust_exhaustion", {"character": "测试勇者", "delta": 1, "reason": "酷寒"})
        self.assertIn("酷寒", out["text"])
        self.assertEqual(char["exhaustion"], 1)


class SkillCheckActionTests(unittest.TestCase):
    def test_proficiency_applied(self):
        room, char = make_room()
        out = actions.perform(room, "skill_check", {"character": "测试勇者", "skill": "perception", "dc": 1})
        self.assertIn("察觉", out["text"])
        check = characters.skill_check(char, "perception", dc=1)
        self.assertEqual(check["skill"], "perception")
        self.assertEqual(check["ability"], "wis")
        self.assertIn("+2", check["breakdown"])                       # 感知 0 + 熟练 2
        self.assertTrue(check["success"])


class RitualAndEffectTests(unittest.TestCase):
    def test_ritual_no_slot(self):
        room, char = make_room()
        char["class_id"] = "wizard"
        char["spells"] = [{"id": "alarm", "name": "警报术", "level": 1},
                          {"id": "fire-bolt", "name": "火焰箭", "level": 0}]
        char["spell_slots"] = {"1": {"max": 2, "used": 0}}
        actions.perform(room, "cast_spell", {"caster": "测试勇者", "spell": "alarm", "ritual": True})
        self.assertEqual(char["spell_slots"]["1"]["used"], 0)          # 仪式不耗法术位
        self.assertGreaterEqual(room.clock_minutes, 10)                # 时钟自动推进 10 分钟
        with self.assertRaises(ValueError):
            actions.perform(room, "cast_spell", {"caster": "测试勇者", "spell": "fire-bolt", "ritual": True})   # 非仪式法术不能按仪式施放

    def test_same_effect_no_stacking(self):
        room, char = make_room()
        spells.add_effect(char, "祝福术", 10, 0)
        text = spells.add_effect(char, "祝福术", 5, 0)
        self.assertIn("不叠加", text)
        self.assertEqual(len(char["effects"]), 1)


class DowntimeTests(unittest.TestCase):
    def _rich(self):
        room, char = make_room()
        char["coins"] = {"pp": 0, "gp": 1000, "sp": 0, "cp": 0}
        return room, char

    def test_working_income(self):
        room, char = self._rich()
        out = actions.perform(room, "downtime", {"character": "测试勇者", "activity": "working", "weeks": 1, "skill": "survival"})
        self.assertGreater(characters.coin_total_cp(char["coins"]), 100000)
        self.assertGreaterEqual(room.clock_minutes, 7 * 1440)

    def test_carousing_cost_and_clock(self):
        room, char = self._rich()
        out = actions.perform(room, "downtime", {"character": "测试勇者", "activity": "carousing", "weeks": 1, "tier": "lower"})
        self.assertIn("狂欢", out["text"])
        self.assertEqual(characters.coin_total_cp(char["coins"]), 100000 - 1000)   # 下层狂欢 10 金
        with self.assertRaises(ValueError):
            actions.perform(room, "downtime", {"character": "测试勇者", "activity": "carousing", "tier": "nope"})

    def test_research_cost(self):
        room, char = self._rich()
        out = actions.perform(room, "downtime", {"character": "测试勇者", "activity": "researching", "weeks": 1, "skill": "history", "topic": "白龙"})
        self.assertIn("白龙", out["text"])
        self.assertEqual(characters.coin_total_cp(char["coins"]), 100000 - 5000)   # 研究 50 金

    def test_crafting_completes(self):
        room, char = self._rich()
        out = actions.perform(room, "downtime", {"character": "测试勇者", "activity": "crafting", "item": "dagger"})
        self.assertIn("制作完成", out["text"])
        self.assertTrue(any(it.get("id") == "dagger" for it in char["items"]))
        self.assertIsNone(char["crafting"])

    def test_training_progress(self):
        room, char = self._rich()
        out = actions.perform(room, "downtime", {"character": "测试勇者", "activity": "training", "weeks": 1, "goal": "盗贼工具"})
        self.assertIn("训练", out["text"])
        self.assertIsNotNone(char["training"])

    def test_downtime_blocks_dying(self):
        room, char = self._rich()
        char["dying"] = True
        with self.assertRaises(ValueError):
            actions.perform(room, "downtime", {"character": "测试勇者", "activity": "working"})


class VehicleTests(unittest.TestCase):
    def _with_galley(self):
        room, char = make_room()
        char["coins"] = {"pp": 0, "gp": 20000, "sp": 0, "cp": 0}
        char.setdefault("items", []).append({"id": "galley", "name": "桨帆船", "qty": 1})
        return room, char

    def test_damage_threshold_and_destroy(self):
        room, char = self._with_galley()
        out = actions.perform(room, "damage_vehicle", {"character": "测试勇者", "vehicle": "galley", "delta": -10, "single_hit": True})
        self.assertIn("低于伤害阈值", out["text"])
        actions.perform(room, "damage_vehicle", {"character": "测试勇者", "vehicle": "galley", "delta": -30, "single_hit": True})
        item = next(it for it in char["items"] if it.get("id") == "galley")
        self.assertEqual(item["hp"], item["hp_max"] - 30)
        actions.perform(room, "damage_vehicle", {"character": "测试勇者", "vehicle": "galley", "delta": -9999})
        self.assertEqual(item["hp"], 0)

    def test_repair(self):
        room, char = self._with_galley()
        item = next(it for it in char["items"] if it.get("id") == "galley")
        actions.perform(room, "damage_vehicle", {"character": "测试勇者", "vehicle": "galley", "delta": -200})
        before = characters.coin_total_cp(char["coins"])
        out = actions.perform(room, "repair_vehicle", {"character": "测试勇者", "vehicle": "galley", "hours": 2})
        self.assertIn("修理", out["text"])
        self.assertEqual(item["hp"], item["hp_max"] - 200 + item["ac"] * 2)
        self.assertEqual(characters.coin_total_cp(char["coins"]), before - (3000000 // 10) * 2)
        # 摧毁后无法修理
        actions.perform(room, "damage_vehicle", {"character": "测试勇者", "vehicle": "galley", "delta": -99999})
        with self.assertRaises(ValueError):
            actions.perform(room, "repair_vehicle", {"character": "测试勇者", "vehicle": "galley"})


class HitDiceTests(unittest.TestCase):
    """生命骰池（BFRD）：短休消耗回血、长休恢复一半。"""

    def test_spend_and_recover(self):
        room, char = make_room()
        char["hp"]["cur"] = 1
        out = actions.perform(room, "spend_hit_dice", {"character": "测试勇者", "count": 1})
        self.assertEqual(char["hit_dice_used"], 1)
        self.assertGreater(char["hp"]["cur"], 1)
        self.assertIn("生命骰", out["text"])
        # 无法超上限
        with self.assertRaises(ValueError):
            actions.perform(room, "spend_hit_dice", {"character": "测试勇者", "count": 99})
        # 长休恢复一半（1 级 → 至少 1 颗）
        state_mod.long_rest(room)
        self.assertEqual(char["hit_dice_used"], 0)

    def test_dying_blocked(self):
        room, char = make_room()
        char["dying"] = True
        char["hp"]["cur"] = 0
        with self.assertRaises(ValueError):
            actions.perform(room, "spend_hit_dice", {"character": "测试勇者", "count": 1})


class SpellBucketTests(unittest.TestCase):
    """法术三分层（BFRD）：戏法/已知或准备/法术书/仪式 的习得、上限、替换与施放校验。"""

    def _wizard(self):
        char = characters.compute_character({
            "name": "分层法师", "race": "human", "klass": "wizard", "background": "scholar",
            "method": "pointbuy", "base": {"str": 8, "dex": 14, "con": 13, "int": 15, "wis": 12, "cha": 10},
            "skills": ["arcana", "history"], "background_skills": ["nature", "religion"],
            "background_talent": "ritualist", "armor": "none", "shield": False, "backstory": "",
            "cantrips": ["fire-bolt", "light", "mage-hand"],
            "spells": ["magic-missile", "shield", "mage-armor"],
            "rituals": ["alarm"],
        })
        char["level"] = 3
        char["spell_slots"] = {"1": {"max": 4, "used": 0}, "2": {"max": 2, "used": 0}}
        return char

    def test_creation_buckets(self):
        char = self._wizard()
        self.assertEqual(len(char["cantrips"]), 3)
        self.assertEqual(len(char["spellbook"]), 3)
        self.assertEqual(char["spellbook_credits"], 3)
        self.assertEqual([e["id"] for e in char["rituals"]], ["alarm"])
        self.assertTrue(all(e["id"] in {x["id"] for x in char["prepared"]} for e in char["spellbook"]))

    def test_prepared_required_to_cast(self):
        char = self._wizard()
        # 未准备的法术（放书里但从准备列表移除）不能施放
        char["prepared"] = [e for e in char["prepared"] if e["id"] != "shield"]
        import server.spells as spells_mod
        spells_mod.rebuild_spell_union(char)
        with self.assertRaises(ValueError):
            spells_mod.cast_spell(char, "shield")
        ok, _ = spells_mod.cast_spell(char, "magic-missile")
        self.assertIn("魔法飞弹", ok)

    def test_learn_with_credit_and_cap(self):
        import server.spells as spells_mod
        char = self._wizard()
        sid, entry = spells_mod.find_spell("burning-hands")
        text = spells_mod.learn_spell(char, sid, entry)      # 自动落法术书并扣名额
        self.assertIn("法术书", text)
        self.assertEqual(char["spellbook_credits"], 2)
        with self.assertRaises(ValueError):
            spells_mod.learn_spell(char, *spells_mod.find_spell("burning-hands"))

    def test_ritual_slot_requires_ritual_bucket(self):
        import server.spells as spells_mod
        char = self._wizard()
        with self.assertRaises(ValueError):
            spells_mod.cast_spell(char, "mage-armor", as_ritual=True)   # 非仪式法术
        with self.assertRaises(ValueError):
            spells_mod.cast_spell(char, "shield", as_ritual=True)       # 不在仪式栏位
        summary, _ = spells_mod.cast_spell(char, "alarm", as_ritual=True)
        self.assertIn("仪式", summary)

    def test_prepare_requires_spellbook(self):
        """法师只能从法术书准备；入书后可准备（补 UI 谓词修复的引擎兜底）。"""
        import server.spells as spells_mod
        char = self._wizard()
        sid, entry = spells_mod.find_spell("feather-fall")
        with self.assertRaises(ValueError):
            spells_mod.learn_spell(char, sid, entry, bucket="prepared")   # 不在法术书
        spells_mod.learn_spell(char, sid, entry, bucket="spellbook")
        spells_mod.learn_spell(char, sid, entry, bucket="prepared")
        self.assertTrue(any(e["id"] == sid for e in char["prepared"]))

    def test_known_swap(self):
        import server.spells as spells_mod
        char = characters.compute_character({
            "name": "分层诗人", "race": "human", "klass": "bard", "background": "criminal",
            "method": "pointbuy", "base": {"str": 8, "dex": 14, "con": 13, "int": 10, "wis": 12, "cha": 15},
            "skills": ["performance", "persuasion", "history"], "background_skills": ["stealth", "insight"],
            "background_talent": "covert", "armor": "leather", "shield": False, "backstory": "",
            "cantrips": ["vicious-mockery", "light", "mage-hand"],
            "spells": ["charm", "color-spray", "grease", "thunderwave"],
            "rituals": [],
        })
        char["swap_credits"] = 1
        summary = spells_mod.swap_spell(char, "charm", *spells_mod.find_spell("sleep"))
        self.assertIn("替换法术", summary)
        self.assertEqual(char["swap_credits"], 0)
        self.assertTrue(any(e["id"] == "sleep" for e in char["known"]))
        with self.assertRaises(ValueError):
            spells_mod.swap_spell(char, "sleep", *spells_mod.find_spell("magic-missile"))   # 名额用尽

    def test_cleric_prepared_track(self):
        import server.spells as spells_mod
        char = characters.compute_character({
            "name": "分层牧师", "race": "human", "klass": "cleric", "background": "soldier",
            "method": "pointbuy", "base": {"str": 12, "dex": 10, "con": 14, "int": 8, "wis": 15, "cha": 13},
            "skills": ["religion", "insight"], "background_skills": ["athletics", "medicine"],
            "background_talent": "field-medic", "armor": "scale", "shield": True, "backstory": "",
            "cantrips": ["sacred-flame", "light", "guidance"],
            "spells": ["cure-wounds", "bless"],
            "rituals": ["purify-food-and-drink"],
        })
        self.assertEqual(len(char["prepared"]), 2)
        self.assertEqual(char["prepared"][0]["id"], "cure-wounds")
        # 准备上限 = 感知调整值 + 等级（+2 + 1 = 3）
        limits = spells_mod.limits_for("cleric", 1, char)
        self.assertEqual(limits["prepared"], 3)
        sid, entry = spells_mod.find_spell("shield-of-faith")
        spells_mod.learn_spell(char, sid, entry)               # 自动进准备栏
        self.assertEqual(len(char["prepared"]), 3)
        with self.assertRaises(ValueError):
            spells_mod.learn_spell(char, *spells_mod.find_spell("guiding-bolt"))   # 准备已满

    def test_migration_from_legacy(self):
        import server.spells as spells_mod
        legacy = {
            "name": "老法爷", "class_id": "wizard", "level": 3,
            "mods": {"int": 4}, "spells": [
                {"id": "fire-bolt", "name": "火焰箭", "level": 0},
                {"id": "magic-missile", "name": "魔法飞弹", "level": 1},
                {"id": "shield", "name": "护盾术", "level": 1},
            ],
        }
        state_mod._migrate_spell_buckets(legacy)
        self.assertEqual(legacy["spell_model"], 2)
        self.assertEqual([e["id"] for e in legacy["cantrips"]], ["fire-bolt"])
        self.assertEqual({e["id"] for e in legacy["spellbook"]}, {"magic-missile", "shield"})
        self.assertEqual(legacy["spellbook_credits"], 8)       # 3 级应有 6+2×2=10 条名额，已有 2 条
        self.assertEqual(len(legacy["prepared"]), 2)

    def test_migration_removes_spellbook_from_non_wizard(self):
        """旧存档迁移：非法师清掉法术书字段，法师保留（v0.6.28）。"""
        cleric = {
            "class_id": "cleric", "level": 3, "spell_model": 2,
            "spellbook": [], "spellbook_credits": 0,
            "spells": [], "cantrips": [], "prepared": [], "rituals": [],
            "mods": {"wis": 2},
        }
        state_mod.migrate_character_fields(cleric)
        self.assertNotIn("spellbook", cleric)
        self.assertNotIn("spellbook_credits", cleric)

        wizard = {
            "class_id": "wizard", "level": 3, "spell_model": 2,
            "spellbook": [{"id": "shield", "name": "护盾术", "level": 1}],
            "spellbook_credits": 4,
            "spells": [], "cantrips": [], "prepared": [], "rituals": [],
            "mods": {"int": 4},
        }
        state_mod.migrate_character_fields(wizard)
        self.assertIn("spellbook", wizard)
        self.assertEqual(wizard["spellbook_credits"], 4)


class ArmorSystemTests(unittest.TestCase):
    """护甲数据驱动 + 穿着推导 + 换装（v0.6.5）。"""

    def test_armor_data_complete(self):
        armors = characters.armor_items()
        names = {v["label"] for v in armors.values()}
        for label in ("软甲", "皮甲", "镶钉皮甲", "铆钉甲", "兽皮甲", "链甲衫",
                      "鳞甲", "胸甲", "半身板甲", "环甲", "锁子甲", "板条甲", "全身板甲", "盾牌"):
            self.assertIn(label, names, label)
        self.assertEqual(armors["breastplate"]["base"], 14)
        self.assertEqual(armors["breastplate"]["dex"], 2)
        self.assertEqual(armors["ring-mail"]["dex"], 0)
        self.assertTrue(armors["shield"]["is_shield"])

    def test_worn_derived_from_equipment(self):
        """车卡：穿着从装备选项推导（锁子甲 + 盾 → AC 18；法师无甲 → 10+敏捷）。"""
        fighter = characters.compute_character({
            "name": "推导战士", "race": "human", "klass": "fighter", "background": "soldier",
            "method": "pointbuy", "base": {"str": 15, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 8},
            "skills": ["perception", "insight"], "background_skills": ["athletics", "survival"],
            "background_talent": "combat-conditioning", "backstory": "",
            "equipment_choices": [
                {"index": 0, "key": "a", "weapons": []},
                {"index": 1, "key": "a", "weapons": ["longsword"]},
                {"index": 2, "key": "a", "weapons": []},
                {"index": 3, "key": "a", "weapons": []},
            ],
        })
        self.assertEqual(fighter["worn_armor"], "chain-mail")
        self.assertTrue(fighter["worn_shield"])
        self.assertEqual(fighter["ac"], 18)
        wizard = characters.compute_character({
            "name": "推导法师", "race": "human", "klass": "wizard", "background": "scholar",
            "method": "pointbuy", "base": {"str": 8, "dex": 14, "con": 13, "int": 15, "wis": 12, "cha": 10},
            "skills": ["arcana", "history"], "background_skills": ["nature", "religion"],
            "background_talent": "ritualist", "backstory": "",
            "cantrips": [], "spells": [], "rituals": [],
            "equipment_choices": [
                {"index": 0, "key": "a", "weapons": []},
                {"index": 1, "key": "a", "weapons": []},
                {"index": 2, "key": "a", "weapons": []},
                {"index": 3, "key": "", "weapons": []},
            ],
        })
        self.assertEqual(wizard["worn_armor"], "none")
        self.assertEqual(wizard["ac"], 12)              # 10 + 敏捷 2

    def test_wear_armor_action(self):
        room, char = make_room()
        char.setdefault("items", []).append({"id": "breastplate", "name": "胸甲", "qty": 1})
        out = actions.perform(room, "wear_armor", {"character": "测试勇者", "armor": "breastplate", "shield": False})
        self.assertIn("胸甲", out["text"])
        self.assertEqual(char["worn_armor"], "breastplate")
        self.assertEqual(char["ac"], 16)                # 14 + 敏捷 2（上限 2）
        with self.assertRaises(ValueError):
            actions.perform(room, "wear_armor", {"character": "测试勇者", "armor": "plate"})   # 未持有


if __name__ == "__main__":
    unittest.main()
