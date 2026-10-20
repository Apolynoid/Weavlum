import unittest

from server import characters, srd

BASE = {"str": 15, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 8}


def fighter_payload(**overrides):
    payload = {
        "name": "测试者",
        "race": "dwarf",
        "klass": "fighter",
        "background": "soldier",
        "method": "pointbuy",
        "base": dict(BASE),
        "skills": ["perception", "insight"],
        "background_skills": ["athletics", "survival"],
        "background_talent": "combat-conditioning",
        "armor": "chain_mail",
        "shield": True,
        "backstory": "",
    }
    payload.update(overrides)
    return payload


class ComputeTests(unittest.TestCase):
    def test_fighter_derived_stats(self):
        char = characters.compute_character(fighter_payload())
        self.assertEqual(char["abilities"]["con"], 13)          # BFRD：血统不提供属性加值
        self.assertEqual(char["hp"]["max"], 13)                 # 1d10 + 体质 1 + 战斗锤炼每级 +2
        self.assertEqual(char["ac"], 18)                        # 链甲 16 + 盾 2
        self.assertEqual(char["saves"]["str"], 4)
        self.assertEqual(char["saves"]["con"], 3)
        self.assertEqual(char["skills"]["perception"], 2)
        self.assertEqual(char["passive_perception"], 12)
        self.assertEqual(char["speed"], 30)                     # BFRD：矮人速度 30
        self.assertIn("combat-conditioning", char["talents"])
        self.assertEqual(char["luck"], 0)
        self.assertTrue(char["equipment"])                      # 职业装备行入包（文本过渡）

    def test_wizard_spells_at_creation(self):
        char = characters.compute_character(
            fighter_payload(
                klass="wizard",
                skills=["arcana", "history"],
                armor="none",
                shield=False,
                spells=["fire-bolt", "magic-missile", "shield"],
                spell_mode="class",
            )
        )
        self.assertEqual(char["spell_slots"], {"1": {"max": 2, "used": 0}})
        self.assertEqual({s["name"] for s in char["spells"]}, {"火焰箭", "魔法飞弹", "护盾术"})
        self.assertEqual(char["spell_mode"], "class")
        self.assertEqual(char["effects"], [])
        # v0.6.4 分层：戏法入戏法栏；1 环入法术书并可自动准备；免费名额补足
        self.assertEqual([e["id"] for e in char["cantrips"]], ["fire-bolt"])
        self.assertEqual({e["id"] for e in char["spellbook"]}, {"magic-missile", "shield"})
        self.assertEqual({e["id"] for e in char["prepared"]}, {"magic-missile", "shield"})
        self.assertEqual(char["spellbook_credits"], 4)
        self.assertEqual(char["spell_model"], 2)
        self.assertIsNone(char["concentration"])

    def test_spellbook_is_wizard_only(self):
        """法术书只属于法师：非施法者与牧师建卡都不写 spellbook / spellbook_credits（v0.6.28）。"""
        fighter = characters.compute_character(fighter_payload())
        self.assertNotIn("spellbook", fighter)
        self.assertNotIn("spellbook_credits", fighter)
        cleric = characters.compute_character(fighter_payload(
            klass="cleric",
            base={"str": 12, "dex": 10, "con": 14, "int": 8, "wis": 15, "cha": 13},
            skills=["religion", "insight"],
            background_skills=["athletics", "medicine"],
            background_talent="field-medic",
            armor="scale",
            shield=True,
            cantrips=["sacred-flame", "light", "guidance"],
            spells=["cure-wounds", "bless"],
            rituals=["purify-food-and-drink"],
        ))
        self.assertNotIn("spellbook", cleric)
        self.assertNotIn("spellbook_credits", cleric)

    def test_wizard_spell_validation_errors(self):
        with self.assertRaises(ValueError):
            characters.compute_character(
                fighter_payload(
                    klass="wizard", skills=["arcana", "history"], armor="none", shield=False,
                    spells=["fireball"], spell_mode="class",
                )
            )
        with self.assertRaises(ValueError):
            characters.compute_character(
                fighter_payload(
                    klass="wizard", skills=["arcana", "history"], armor="none", shield=False,
                    spells=["bless"], spell_mode="class",
                )
            )

    def test_non_caster_empty_spell_fields(self):
        char = characters.compute_character(fighter_payload())
        self.assertEqual(char["spells"], [])
        self.assertEqual(char["spell_slots"], {})
        self.assertIsNone(char["spell_mode"])

    def test_wizard_spell_stats(self):
        char = characters.compute_character(
            fighter_payload(klass="wizard", skills=["arcana", "history"], armor="none", shield=False)
        )
        self.assertEqual(char["spellcasting"]["ability"], "int")
        self.assertEqual(char["spellcasting"]["save_dc"], 8 + 2 + 1)
        self.assertEqual(char["spellcasting"]["attack_bonus"], 2 + 1)

    def test_point_buy_over_budget(self):
        bad = {"str": 15, "dex": 15, "con": 15, "int": 15, "wis": 15, "cha": 15}
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(method="pointbuy", base=bad))

    def test_point_buy_ok(self):
        char = characters.compute_character(fighter_payload(method="pointbuy"))
        self.assertEqual(char["method"], "pointbuy")

    def test_standard_array_mismatch(self):
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(
                method="standard",
                base={"str": 16, "dex": 14, "con": 14, "int": 13, "wis": 10, "cha": 9},
            ))

    def test_skill_overlap_with_background(self):
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(skills=["athletics", "perception"]))

    def test_wrong_skill_count(self):
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(skills=["perception"]))

    def test_unknown_skill_choice(self):
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(skills=["stealth", "perception"]))


class MathTests(unittest.TestCase):
    def test_modifier(self):
        self.assertEqual(characters.modifier(10), 0)
        self.assertEqual(characters.modifier(8), -1)
        self.assertEqual(characters.modifier(9), -1)
        self.assertEqual(characters.modifier(15), 2)
        self.assertEqual(characters.modifier(20), 5)

    def test_point_buy_cost(self):
        self.assertEqual(characters.point_buy_cost(BASE), 27)
        self.assertEqual(characters.point_buy_cost({"str": 8, "dex": 8, "con": 8, "int": 8, "wis": 8, "cha": 8}), 0)


class BfrdCreationTests(unittest.TestCase):
    """BFRD 建卡要素：性别 / 背景天赋校验 / 传承与自由天赋 / 标准数组。"""

    def test_gender_optional(self):
        char = characters.compute_character(fighter_payload(gender="女"))
        self.assertEqual(char["gender"], "女")

    def test_background_talent_validation(self):
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(background_talent="arcanist"))
        char = characters.compute_character(fighter_payload(background_talent="field-medic"))
        self.assertIn("field-medic", char["talents"])

    def test_heritage_and_free_talent(self):
        char = characters.compute_character(fighter_payload(heritage="cottage", talent="athletic"))
        self.assertEqual(char["heritage"], "村舍")
        self.assertIn("athletic", char["talents"])
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(heritage="nope"))
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(talent="not-a-talent"))

    def test_bard_any_skills(self):
        """吟游诗人：任意三项职业技能可正常建卡（skill_choices='any'）。"""
        char = characters.compute_character(fighter_payload(
            klass="bard",
            skills=["history", "deception", "performance"],
            armor="leather",
        ))
        for sid in ("history", "deception", "performance"):
            self.assertIn(sid, char["skills"])
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(
                klass="bard",
                skills=["history", "deception"],       # 任意模式下限仍是 3 项
                armor="leather",
            ))

    def test_standard_array_bfrd(self):
        char = characters.compute_character(fighter_payload(
            method="standard",
            base={"str": 16, "dex": 14, "con": 14, "int": 13, "wis": 10, "cha": 8},
        ))
        self.assertEqual(char["abilities"]["str"], 16)

    def test_human_extra_skill(self):
        char = characters.compute_character(fighter_payload(race="human", human_skill="stealth"))
        self.assertIn("stealth", char["skills"])
        self.assertEqual(char["human_skill"], "stealth")
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(race="dwarf", human_skill="stealth"))
        with self.assertRaises(ValueError):
            characters.compute_character(fighter_payload(race="human", human_skill="perception"))


class EquipmentChoiceTests(unittest.TestCase):
    """装备选项组：a/b 选择与武器选择位（引擎侧校验与结算的唯一权威）。"""

    def _choices(self, klass="fighter"):
        return characters.build_companion_payload(klass)["equipment_choices"]

    def test_resolve_fighter_choices(self):
        items, texts = characters.resolve_equipment("fighter", self._choices())
        ids = [it.get("id") for it in items]
        self.assertIn("chain-mail", ids)
        self.assertIn("shield", ids)
        self.assertEqual(len(texts), 4)
        self.assertTrue(any(it.get("id") for it in items))

    def test_choice_validation(self):
        with self.assertRaises(ValueError):
            characters.resolve_equipment("fighter", self._choices()[:2])     # 行数不足
        bad = self._choices()
        bad[1]["weapons"] = []                                               # 缺武器
        with self.assertRaises(ValueError):
            characters.resolve_equipment("fighter", bad)
        bad2 = self._choices()
        bad2[0]["key"] = "z"                                                 # 未知选项键 → 回退第一项
        items, _ = characters.resolve_equipment("fighter", bad2)
        self.assertIn("chain-mail", [it.get("id") for it in items])

    def test_compute_with_equipment_choices(self):
        char = characters.compute_character(fighter_payload(equipment_choices=self._choices()))
        ids = [it.get("id") for it in char["items"]]
        self.assertIn("chain-mail", ids)
        self.assertIn("shield", ids)
        self.assertIn("弩矢", "".join(it["name"] for it in char["items"]))


class NumericTalentTests(unittest.TestCase):
    """数值型天赋机制化（v0.5.4 的 8 条）与幸运系统规则。"""

    def _char(self, **overrides):
        payload = {
            "name": "天赋者", "race": "human", "klass": "fighter", "background": "soldier",
            "method": "pointbuy", "base": dict(BASE),
            "skills": ["perception", "insight"], "background_skills": ["athletics", "survival"],
            "background_talent": "combat-conditioning", "armor": "chain_mail", "shield": False,
            "backstory": "",
        }
        payload.update(overrides)
        return characters.compute_character(payload)

    def test_combat_conditioning_hp(self):
        char = self._char()
        self.assertEqual(char["hp"]["max"], 11 + 2)          # 1d10+体质 1，战斗锤炼每级 +2
        from server import spells as spells_mod
        while char["level"] < 4:
            spells_mod.level_up(char)
        per_level = 10 // 2 + 1 + 1                           # 平均生命骰 + 体质
        self.assertEqual(char["hp"]["max"], 13 + 3 * (per_level + 2))

    def test_quick_and_scrutinous(self):
        self.assertEqual(self._char(talent="quick")["speed"], 40)
        self.assertEqual(self._char(talent="scrutinous")["passive_perception"], 12 + 5)

    def test_armor_expert_training_and_school(self):
        plain = self._char()
        self.assertEqual(self._char(talent="armor-expert")["ac"], plain["ac"] + 1)
        self.assertEqual(self._char(talent="armor-expert", armor="none")["ac"],
                         self._char(armor="none")["ac"])      # 无甲不触发
        base16 = {"str": 15, "dex": 16, "con": 13, "int": 12, "wis": 10, "cha": 8}
        trained = self._char(talent="armor-training", armor="chain_shirt", base=base16)
        untrained = self._char(armor="chain_shirt", base=base16)
        self.assertEqual(trained["ac"] - untrained["ac"], 1)  # 中甲敏捷上限 2→3
        with self.assertRaises(ValueError):
            self._char(talent="school-specialization")        # 缺学派
        school = self._char(talent="school-specialization", talent_school="塑能系")
        self.assertEqual(school["talent_schools"]["school-specialization"], "塑能系")

    def test_athletic_carry(self):
        info = characters.carry_info(self._char(talent="athletic"))
        self.assertEqual(info["capacity"], 15 * 18)
        self.assertEqual(info["push"], 15 * 35)

    def test_luck_rules(self):
        char = self._char()
        self.assertEqual(characters.luck_add(char, 1, from_failure=True)[0], 1)
        for _ in range(4):
            characters.luck_add(char, 1)
        self.assertEqual(char["luck"], 5)
        self.assertEqual(characters.luck_add(char, 1, d4_roll=3)[0], 3)   # 满 5 再获得 → 重置 d4
        touch = self._char(talent="touch-of-luck")
        characters.luck_add(touch, 1, from_failure=True)
        self.assertEqual(touch["luck"], 2)                                # 失败获得翻倍
        touch["luck"] = 5
        self.assertEqual(characters.luck_add(touch, 1, d4_roll=2)[0], 3)  # 幸运之触重置 1d4+1
        with self.assertRaises(ValueError):
            characters.luck_spend(char, 4)

    def test_luck_actions_and_failed_roll(self):
        from server import actions as actions_mod
        from tests.test_combat import make_room
        room, char = make_room()
        actions_mod.perform(room, "grant_luck", {"character": "测试勇者", "reason": "测试"})
        self.assertEqual(char["luck"], 1)
        actions_mod.perform(room, "spend_luck", {"character": "测试勇者", "amount": 1})
        self.assertEqual(char["luck"], 0)
        actions_mod.perform(room, "roll_dice", {"expression": "1d20", "dc": 99, "character": "测试勇者"})
        self.assertGreaterEqual(char["luck"], 1)              # 失败检定自动积累 1 点


class AdaptationTests(unittest.TestCase):
    """自然适应（兽裔/小族，v0.6.6）：必选、强健无甲 AC、侏儒种族戏法、替用速度。"""

    def _payload(self, **overrides):
        payload = {
            "name": "适应测试", "race": "beastkin", "klass": "fighter", "background": "soldier",
            "method": "pointbuy", "base": dict(BASE),
            "skills": ["perception", "insight"], "background_skills": ["athletics", "survival"],
            "background_talent": "combat-conditioning", "worn_armor": "none", "backstory": "",
        }
        payload.update(overrides)
        return payload

    def test_data_shape(self):
        races = srd.races()
        self.assertEqual([a["name"] for a in races["beastkin"]["adaptations"]], ["禽类", "灵巧", "水生", "强健"])
        self.assertEqual([a["name"] for a in races["smallfolk"]["adaptations"]], ["侏儒", "半身人"])
        self.assertFalse(races["dwarf"]["adaptations"])

    def test_required_choice(self):
        with self.assertRaises(ValueError):
            characters.compute_character(self._payload())                 # 未选
        with self.assertRaises(ValueError):
            characters.compute_character(self._payload(adaptation="不存在"))
        char = characters.compute_character(self._payload(adaptation="水生"))
        self.assertEqual(char["adaptation"], "水生")
        self.assertEqual(char["alt_speed"], "游泳")

    def test_sturdy_unarmored_ac(self):
        char = characters.compute_character(self._payload(adaptation="强健"))
        self.assertEqual(char["ac"], 15)                                  # 13 + 敏捷 2（无甲）

    def test_gnome_racial_cantrip(self):
        from server import spells as spells_mod
        with self.assertRaises(ValueError):
            characters.compute_character(self._payload(race="smallfolk", adaptation="侏儒"))
        char = characters.compute_character(self._payload(
            race="smallfolk", adaptation="侏儒", adaptation_spell_ability="wis", klass="rogue",
            background="criminal", background_talent="covert",
            skills=["stealth", "perception", "acrobatics", "investigation"],
            background_skills=["insight", "deception"],
        ))
        racial = [c for c in char["cantrips"] if c.get("racial")]
        self.assertEqual([c["id"] for c in racial], ["minor-illusion"])
        self.assertEqual(char["adaptation_spell_ability"], "wis")
        summary, _ = spells_mod.cast_spell(char, "minor-illusion")        # 非施法职业也能施放种族戏法
        self.assertIn("次级幻影", summary)

    def test_no_adaptation_for_other_lineages(self):
        with self.assertRaises(ValueError):
            characters.compute_character(self._payload(race="human", adaptation="水生"))


if __name__ == "__main__":
    unittest.main()
