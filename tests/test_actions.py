import tempfile
import unittest
from pathlib import Path

from server import actions, campaign as campaign_mod
from server import state as state_mod
from tests.test_combat import make_room


class ActionDispatchTests(unittest.TestCase):
    def setUp(self):
        """隔离：角色目录与 meta 文件重定向到临时目录（与 test_combat 一致），不触碰真实数据。"""
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_char_dir = state_mod.CHAR_DIR
        self._orig_meta_file = state_mod.ROOM_META_FILE
        state_mod.CHAR_DIR = Path(self._tmp.name) / "characters"
        state_mod.ROOM_META_FILE = Path(self._tmp.name) / "room_meta.json"

    def tearDown(self):
        state_mod.CHAR_DIR = self._orig_char_dir
        state_mod.ROOM_META_FILE = self._orig_meta_file
        self._tmp.cleanup()

    def test_unknown_action(self):
        room, _ = make_room()
        with self.assertRaises(ValueError):
            actions.perform(room, "not-an-action", {})

    def test_read_actions(self):
        room, _ = make_room()
        out = actions.perform(room, "get_party", {})
        self.assertIn("测试勇者", out["text"])
        self.assertFalse(out["changed"])
        out = actions.perform(room, "get_campaign", {})
        self.assertIn("尚未设置", out["text"])

    def test_update_quest_via_action(self):
        room, _ = make_room()
        room.campaign = campaign_mod.normalize_structure(
            {
                "hub": {"name": "镇"},
                "pressure_type": "threat_clock",
                "pressure": {"label": "威胁", "max": 6, "level": 0},
                "quests": [
                    {"id": "q1", "tier": 1, "title": "A", "location": "L", "objective": "O", "completion": "C"},
                ],
                "finale": {"location": "F", "objective": "O"},
            }
        )
        out = actions.perform(room, "update_quest", {"quest": "q1", "status": "in_progress", "note": "出发"})
        self.assertTrue(out["changed"])
        self.assertTrue(out["notes"])
        self.assertIn("in_progress", out["text"])

    def test_advance_time_via_action(self):
        room, _ = make_room()
        out = actions.perform(room, "advance_time", {"minutes": 10, "reason": "搜索"})
        self.assertAlmostEqual(room.clock_minutes, 10.0)
        self.assertIn("时间推进", out["text"])
        self.assertTrue(out["changed"])

    def test_level_up_via_action(self):
        room, char = make_room()
        out = actions.perform(room, "level_up", {"character": "测试勇者"})
        self.assertEqual(char["level"], 2)
        self.assertTrue(out["changed"])
        self.assertIn("升到 2 级", out["text"])

    def test_level_up_party_via_action(self):
        """全队升级：上桌角色与同伴各升 1 级；达上限者跳过并说明。"""
        room, char = make_room()
        comp = state_mod.create_companion(room, "小弟", "fighter")
        out = actions.perform(room, "level_up_party", {})
        self.assertTrue(out["changed"])
        self.assertEqual(char["level"], 2)
        self.assertEqual(comp["level"], 2)
        char["level"] = 20
        out = actions.perform(room, "level_up_party", {})
        self.assertIn("未能升级", out["text"])
        self.assertEqual(comp["level"], 3)

    def test_query_monsters_cr_range(self):
        """CR 区间筛选：min_cr/max_cr 组合过滤（v0.4.1 全量怪物库）。"""
        room, _ = make_room()
        out = actions.perform(room, "query_monsters", {"min_cr": 10, "max_cr": 12})
        text = out["text"]
        self.assertIn("CR", text)
        self.assertNotIn("id=goblin", text)      # 低 CR 怪不出现在 ≥10 区间
        out = actions.perform(room, "query_monsters", {"name": "goblin", "max_cr": 0.25})
        self.assertIn("id=goblin", out["text"])  # 低 CR 区间仍能查到

    def test_roll_dice_action(self):
        room, _ = make_room()
        out = actions.perform(room, "roll_dice", {"expression": "1d20+3", "reason": "测试"})
        record = out["roll_record"]
        self.assertIsNotNone(record)
        self.assertTrue(4 <= record["total"] <= 23)

    def test_combat_actions_via_action(self):
        room, _ = make_room()
        out = actions.perform(room, "spawn_monsters", {"list": [{"id": "goblin", "count": 1}]})
        self.assertTrue(out["changed"])
        self.assertTrue(room.combat["active"])
        self.assertTrue(out["attach_board"])
        out = actions.perform(room, "end_combat", {"reason": "测试脱离"})
        self.assertFalse(room.combat["active"])
        self.assertIn("测试脱离", out["text"])


if __name__ == "__main__":
    unittest.main()
