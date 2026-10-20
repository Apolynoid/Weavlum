import unittest

from server import campaign


def sample_structure():
    """构造一份通过校验的 9 任务结构（3 阶 × 3），供各测试复用。"""
    quests = []
    for tier in (1, 2, 3):
        for index in range(1, 4):
            quests.append(
                {
                    "id": f"q{tier}{index}",
                    "tier": tier,
                    "title": f"任务{tier}-{index}",
                    "location": "某地",
                    "giver": "镇长",
                    "objective": "目标",
                    "hook": "情报",
                    "secret": "真相",
                    "encounters": "2 只 CR 1/8",
                    "reward": "金币",
                    "completion": "完成条件",
                    "fail": "失败后果",
                }
            )
    return {
        "title": "白龙之患",
        "theme": "冰原",
        "pressure_type": "threat_clock",
        "pressure": {"label": "冰龙的侵蚀", "max": 10, "level": 0},
        "hub": {"name": "潮湾镇", "desc": "渔镇", "giver": "镇长瓦尔"},
        "quests": quests,
        "finale": {
            "location": "冰塔峰",
            "objective": "终结白龙",
            "condition": "条件",
            "endings": ["驱逐", "契约", "猎杀"],
        },
    }


class ValidateTests(unittest.TestCase):
    def test_valid_structure(self):
        self.assertEqual(campaign.validate_structure(sample_structure()), [])

    def test_pressure_default_scales_with_quests(self):
        """未给 max 时按任务数 + 1 留余步（9 任务 → 10）。"""
        data = sample_structure()
        del data["pressure"]["max"]
        out = campaign.normalize_structure(data)
        self.assertEqual(out["pressure"]["max"], 10)

    def test_pressure_max_must_exceed_quests(self):
        """max 必须大于任务数，否则任务全清必然满格、结局路线被锁死。"""
        data = sample_structure()
        data["pressure"]["max"] = 9
        self.assertTrue(any("pressure.max" in e for e in campaign.validate_structure(data)))

    def test_bad_pressure_type(self):
        data = sample_structure()
        data["pressure_type"] = "random"
        self.assertTrue(any("pressure_type" in e for e in campaign.validate_structure(data)))

    def test_missing_fields(self):
        data = sample_structure()
        del data["finale"]
        errors = campaign.validate_structure(data)
        self.assertTrue(any("finale" in e for e in errors))
        data = sample_structure()
        del data["quests"][0]["objective"]
        errors = campaign.validate_structure(data)
        self.assertTrue(any("objective" in e for e in errors))

    def test_duplicate_ids(self):
        data = sample_structure()
        data["quests"][1]["id"] = data["quests"][0]["id"]
        self.assertTrue(any("重复" in e for e in campaign.validate_structure(data)))

    def test_pressure_bounds(self):
        data = sample_structure()
        data["pressure"]["level"] = 99
        self.assertTrue(any("pressure.level" in e for e in campaign.validate_structure(data)))


class ProgressTests(unittest.TestCase):
    def test_unlock_rules(self):
        data = campaign.normalize_structure(sample_structure())
        progress = campaign.quest_progress(data)
        self.assertTrue(progress["tiers"][1]["unlocked"])
        self.assertFalse(progress["tiers"][2]["unlocked"])
        self.assertFalse(progress["finale_unlocked"])
        # 一阶完成 2 个 → 二阶解锁
        data["quests"][0]["status"] = "completed"
        data["quests"][1]["status"] = "completed"
        progress = campaign.quest_progress(data)
        self.assertTrue(progress["tiers"][2]["unlocked"])
        self.assertFalse(progress["tiers"][3]["unlocked"])
        # 三阶完成 2 个 → 终局开启
        data["quests"][7]["status"] = "completed"
        data["quests"][8]["status"] = "completed"
        progress = campaign.quest_progress(data)
        self.assertTrue(progress["finale_unlocked"])

    def test_pressure_full_unlocks_finale(self):
        data = campaign.normalize_structure(sample_structure())
        data["pressure"]["level"] = data["pressure"]["max"]
        progress = campaign.quest_progress(data)
        self.assertTrue(progress["pressure_full"])
        self.assertTrue(progress["finale_unlocked"])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.data = campaign.normalize_structure(sample_structure())

    def test_update_and_pressure_advance(self):
        summary = campaign.update_quest(self.data, "任务1-1", "completed")
        self.assertIn("completed", summary)
        self.assertEqual(self.data["quests"][0]["status"], "completed")
        self.assertEqual(self.data["pressure"]["level"], 1)
        summary = campaign.update_quest(self.data, "q12", "in_progress", "线索指向北岭")
        self.assertIn("in_progress", summary)
        self.assertIn("北岭", self.data["quests"][1]["notes"][0])
        # 重复 completed 不重复推进压力
        campaign.update_quest(self.data, "任务1-1", "completed")
        self.assertEqual(self.data["pressure"]["level"], 1)

    def test_update_invalid(self):
        with self.assertRaises(ValueError):
            campaign.update_quest(self.data, "不存在的任务", "completed")
        with self.assertRaises(ValueError):
            campaign.update_quest(self.data, "任务1-1", "done")

    def test_advance_pressure_caps(self):
        campaign.advance_pressure(self.data, 99, "测试")     # 单次最多 +6
        self.assertEqual(self.data["pressure"]["level"], 6)
        text = campaign.advance_pressure(self.data, 99, "测试")
        self.assertEqual(self.data["pressure"]["level"], 10)  # 上限 10（任务数+1）
        self.assertIn("满格", text)

    def test_world_notes_merge_same_name(self):
        text = campaign.add_world_note(self.data, "npcs", "铁匠老约", "右腿瘸")
        self.assertIn("登记", text)
        campaign.add_world_note(self.data, "npcs", "铁匠老约", "曾在矿洞做工")
        entry = self.data["world"]["npcs"][0]
        self.assertIn("右腿瘸", entry["note"])
        self.assertIn("矿洞", entry["note"])
        with self.assertRaises(ValueError):
            campaign.add_world_note(self.data, "bad", "x", "y")

    def test_public_view_hides_spoilers(self):
        view = campaign.public_view(self.data)
        self.assertIsNone(view["finale"])
        self.assertEqual(view["quests"], [])    # 未发现的任务不下发
        campaign.update_quest(self.data, "任务1-1", "in_progress")
        view = campaign.public_view(self.data)
        self.assertEqual(len(view["quests"]), 1)
        self.assertNotIn("secret", view["quests"][0])

    def test_merge_preserves_runtime_state(self):
        campaign.update_quest(self.data, "任务1-1", "completed")
        campaign.add_world_note(self.data, "clues", "矿洞回声", "夜里有敲击声")
        edited = sample_structure()             # 主机编辑后重新提交（无运行时状态）
        merged = campaign.merge_campaign(self.data, edited)
        quest = campaign.find_quest(merged, "q11")
        self.assertEqual(quest["status"], "completed")
        self.assertEqual(merged["world"]["clues"][0]["name"], "矿洞回声")
        self.assertEqual(merged["pressure"]["level"], 1)

    def test_level_target_official_rule(self):
        data = campaign.normalize_structure(sample_structure())
        self.assertEqual(campaign.level_target(data), 1)
        # 初始任务（1 阶）：第 1 个 → 2 级；第 2 个 → 3 级；第 3 个不再升
        campaign.update_quest(data, "q11", "completed")
        self.assertEqual(campaign.level_target(data), 2)
        campaign.update_quest(data, "q12", "completed")
        self.assertEqual(campaign.level_target(data), 3)
        campaign.update_quest(data, "q13", "completed")
        self.assertEqual(campaign.level_target(data), 3)
        # 后续任务（2/3 阶）：每 2 项 +1
        campaign.update_quest(data, "q21", "completed")
        self.assertEqual(campaign.level_target(data), 3)
        campaign.update_quest(data, "q22", "completed")
        self.assertEqual(campaign.level_target(data), 4)
        campaign.update_quest(data, "q23", "completed")
        campaign.update_quest(data, "q31", "completed")
        self.assertEqual(campaign.level_target(data), 5)
        # 终局（击杀/驱离白龙）：额外 +1，封顶 6
        summary = campaign.update_quest(data, "终局", "completed")
        self.assertIn("终局达成", summary)
        self.assertIn("里程碑", summary)
        self.assertTrue(data["finale_cleared"])
        self.assertEqual(campaign.level_target(data), 6)
        summary = campaign.update_quest(data, "终局", "completed")
        self.assertIn("此前已标记过", summary)

    def test_milestone_hint_text(self):
        data = campaign.normalize_structure(sample_structure())
        summary = campaign.update_quest(data, "q11", "completed")
        self.assertIn("里程碑", summary)      # 1 → 2 级
        summary = campaign.update_quest(data, "q12", "completed")
        self.assertIn("里程碑", summary)      # 2 → 3 级
        summary = campaign.update_quest(data, "q13", "completed")
        self.assertNotIn("里程碑", summary)   # 已在 3 级，初始任务不再给升级

    def test_render_brief_contains_core_fields(self):
        brief = campaign.render_brief(self.data)
        self.assertIn("战役结构", brief)
        self.assertIn("潮湾镇", brief)
        self.assertIn("冰塔峰", brief)
        self.assertIn("q11", brief)


class ResetTests(unittest.TestCase):
    def test_reset_progress_keeps_structure(self):
        """重置只清运行时状态；结构文案与任务数量原样保留。"""
        data = campaign.normalize_structure(sample_structure())
        data["quests"][0]["status"] = "in_progress"
        data["quests"][0]["notes"] = ["线索A"]
        data["quests"][1]["status"] = "completed"
        data["pressure"]["level"] = 3
        data["world"]["npcs"].append({"name": "某人", "note": "说明", "ts": 1})
        data["finale_cleared"] = True
        self.assertTrue(campaign.reset_progress(data))
        self.assertEqual(data["quests"][0]["status"], "undiscovered")
        self.assertEqual(data["quests"][0]["notes"], [])
        self.assertEqual(data["quests"][1]["status"], "undiscovered")
        self.assertEqual(data["pressure"]["level"], 0)
        self.assertEqual(data["world"]["npcs"], [])
        self.assertFalse(data["finale_cleared"])
        self.assertEqual(len(data["quests"]), 9)
        self.assertEqual(data["quests"][0]["title"], "任务1-1")
        self.assertEqual(data["title"], "白龙之患")

    def test_reset_progress_without_campaign(self):
        self.assertFalse(campaign.reset_progress(None))


    def test_render_dm_brief_readable_sections(self):
        """GM 阅读版：分阶分组、状态标记、完成项压缩单行、含终局与世界登记。"""
        data = campaign.normalize_structure(sample_structure())
        campaign.update_quest(data, "q11", "completed")
        data["world"]["clues"].append({"name": "线索A", "note": "说明", "ts": 1})
        text = campaign.render_dm_brief(data)
        self.assertIn("GM 秘闻", text)
        self.assertIn("任务1-1", text)
        self.assertIn("一阶任务", text)
        self.assertIn("世界登记", text)
        self.assertIn("终局（未开启）", text)
        self.assertIn("线索A", text)
        # 已完成任务压缩为单行：有 ✓ 标记但没有展开字段
        completed_line = next(ln for ln in text.splitlines() if "任务1-1" in ln)
        self.assertIn("✓", completed_line)
        self.assertNotIn("真相：", completed_line)
        # 未发现任务展开全字段
        self.assertIn("目标：目标", text)

    def test_render_dm_brief_empty(self):
        self.assertEqual(campaign.render_dm_brief(None), "")


if __name__ == "__main__":
    unittest.main()
