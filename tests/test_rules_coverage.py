"""
规则覆盖率测试（v0.6.0「0 规则书查阅」）：
神祇数据、载具条目、《规则速查》模块、状态/特性文本在提示词与界面数据中的露面。
"""

import unittest

from server import actions, ai, rules_ref, srd
from tests.test_combat import make_room


class RulebookCoverageTests(unittest.TestCase):
    def test_classes_skill_choices_complete(self):
        """每个职业都必须有职业技能可选（吟游诗人=任意三项，修 v0.6.2 的解析遗漏）。"""
        classes = srd.classes()
        for cid, klass in classes.items():
            self.assertTrue(klass.get("skill_choices"), f"{cid} 缺职业技能表")
        self.assertEqual(classes["bard"]["skill_choices"], "any")
        self.assertEqual(classes["bard"]["skill_count"], 3)

    def test_subclass_descriptions(self):
        """子职业导语（3 级选择处的悬停素材）：14 个全部可用 + 内容抽查。"""
        classes = srd.classes()
        total = 0
        for cid, klass in classes.items():
            for sid, sub in (klass.get("subclasses") or {}).items():
                total += 1
                self.assertGreaterEqual(len(sub.get("desc") or ""), 10, f"{cid}/{sid} 缺导语")
        self.assertEqual(total, 14)
        self.assertIn("知识", classes["bard"]["subclasses"]["lore"]["desc"])
        self.assertIn("生命", classes["cleric"]["subclasses"]["life-domain"]["desc"])

    def test_feature_attribution_spots(self):
        """特性归属/等级抽查（v0.6.10 全职业审计后的回归锚点）。"""
        classes = srd.classes()
        # 多档特性取首档
        self.assertEqual(classes["fighter"]["features"]["action-surge"]["level"], 2)
        self.assertEqual(classes["barbarian"]["features"]["relentless-rage"]["level"], 14)
        self.assertEqual(classes["druid"]["features"]["improved-beast-form"]["level"], 5)
        # 同名 slug：本职与子职业各归其位（牧师引导神力）
        self.assertEqual(classes["cleric"]["features"]["channel-divinity"]["level"], 2)
        self.assertEqual(classes["cleric"]["subclasses"]["life-domain"]["features"]["channel-divinity"]["level"], 3)
        # 圣武士/邪术师曾整段丢失的特性已补回
        self.assertEqual(classes["paladin"]["features"]["channel-divinity"]["level"], 13)
        self.assertEqual(classes["warlock"]["features"]["pact-magic"]["level"], 13)
        # 子职业特性不得泄漏进本职；机械师秘法金属归冶金师
        self.assertNotIn("mystic-metal", classes["mechanist"]["features"])
        self.assertIn("mystic-metal", classes["mechanist"]["subclasses"]["metallurgist"]["features"])
        # 同名 slug 允许本职与子职业共存（同名不同特性：吟游演出/引导神力），归属由标记校验（check_data）
        self.assertEqual(classes["bard"]["features"]["bardic-performance"]["level"], 2)
        self.assertEqual(classes["bard"]["subclasses"]["lore"]["features"]["bardic-performance"]["level"], 3)

    def test_classes_progression_tables(self):
        """职业进阶表：13 职业 × 20 级全列解析（含职业专属列）。"""
        classes = srd.classes()
        self.assertEqual(len(classes), 13)
        for cid, klass in classes.items():
            prog = klass.get("progression") or {}
            rows = prog.get("rows") or {}
            self.assertEqual(len(rows), 20, cid)
            keys = {c.get("key") for c in prog.get("columns") or []}
            self.assertTrue({"level", "pb", "features"} <= keys, cid)
        self.assertEqual(classes["barbarian"]["progression"]["rows"]["1"]["rage_damage"], "+2")
        self.assertEqual(classes["rogue"]["progression"]["rows"]["1"]["sneak_attack"], "1d6")
        self.assertEqual(classes["sorcerer"]["progression"]["rows"]["1"]["sorcery_points"], 2)
        self.assertEqual(classes["monk"]["progression"]["rows"]["1"]["martial_arts"], "1d4")
        self.assertEqual(classes["druid"]["progression"]["rows"]["2"]["beast_forms"], 2)

    def test_next_level_preview(self):
        """队伍规则资料附"下一级将获得"行（供 AI/玩家决策）。"""
        room, _ = make_room()
        text = actions.party_features_brief(room)
        self.assertIn("下一级（2 级）将获得", text)

    def test_gods_data(self):
        gods = srd.gods()
        self.assertEqual(len(gods), 3)
        total = sum(len(p.get("deities") or []) for p in gods.values())
        self.assertGreaterEqual(total, 35)
        sample = gods["egyptian-pantheon"]["deities"][0]
        self.assertTrue(sample["name"] and sample["domains"] and sample["symbol"])

    def test_vehicles_in_items(self):
        vehicles = {k: v for k, v in srd.items().items() if v.get("category_zh") == "载具"}
        self.assertIn("galley", vehicles)
        self.assertEqual(vehicles["galley"]["name"], "桨帆船")
        self.assertGreater(len(vehicles["galley"]["desc_zh"]), 100)   # 数据块文本已入说明

    def test_rules_reference_sections(self):
        titles = rules_ref.section_titles()
        for key in ("降至 0 点生命值（濒死与死亡）", "冒险之间（Downtime）", "仪式施法",
                    "载具（规则要点）", "存在位面", "多职业（平台声明）"):
            self.assertIn(key, titles)
        text = rules_ref.rules_reference_text()
        self.assertIn("死亡豁免", text)
        self.assertIn("50 周", text)          # 训练时长
        self.assertIn("250 金", text)         # 狂欢上层阶级花费

    def test_briefs(self):
        self.assertIn("阿努比斯", actions.gods_brief())
        self.assertIn("目盲", actions.conditions_brief())
        payload = actions.class_features_payload()
        self.assertGreaterEqual(sum(len(v["subclasses"]) for v in payload.values()), 14)
        self.assertTrue(any(f["desc"] for f in payload["fighter"]["features"]))

    def test_prompt_includes_coverage(self):
        room, char = make_room()
        room.world = ""
        room.summary = ""
        room.dm_note = ""
        prompt = ai.build_system_prompt(room)
        for marker in ("【规则速查", "【状态速查", "【神祇速查", "【队伍规则资料", "死亡豁免", "目盲"):
            self.assertIn(marker, prompt)
        self.assertIn(char["name"], prompt)


if __name__ == "__main__":
    unittest.main()
