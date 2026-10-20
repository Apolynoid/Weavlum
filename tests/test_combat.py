import tempfile
import unittest
from pathlib import Path

from server import campaign as campaign_mod, characters, srd
from server import spells as spells_mod
from server import state as state_mod
from server.state import RoomState

BASE = {"str": 15, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 8}


def make_room():
    room = RoomState()
    room.characters.clear()
    room.participants.clear()
    room.persist_character = lambda char: None
    room.persist_room_meta = lambda: None
    room.seq = 1    # 隔离：之后的自动生成 id 从 c2 起，不碰真实磁盘文件
    room.clock_minutes = 0.0    # 隔离：不沿用真实 room_meta.json 里的时钟
    char = characters.compute_character(
        {
            "name": "测试勇者",
            "race": "human",
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
    )
    char["id"] = "c1"
    char["owner_sid"] = "s1"
    char["owner_nick"] = "测试"
    room.characters["c1"] = char
    room.participants["s1"] = {"nickname": "测试", "connected": True, "char_id": "c1", "is_host": True}
    return room, char


class CombatStateTests(unittest.TestCase):
    def setUp(self):
        """隔离：把角色目录与 meta 文件重定向到临时目录，测试绝不触碰真实数据。"""
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_char_dir = state_mod.CHAR_DIR
        self._orig_meta_file = state_mod.ROOM_META_FILE
        state_mod.CHAR_DIR = Path(self._tmp.name) / "characters"
        state_mod.ROOM_META_FILE = Path(self._tmp.name) / "room_meta.json"

    def tearDown(self):
        state_mod.CHAR_DIR = self._orig_char_dir
        state_mod.ROOM_META_FILE = self._orig_meta_file
        self._tmp.cleanup()

    def test_start_combat_fields(self):
        room, char = make_room()
        state_mod.start_combat(room)
        combatant = room.combat["combatants"][0]
        self.assertEqual(combatant["kind"], "pc")
        self.assertEqual(combatant["speed"], char["speed"])
        self.assertIn("pos", combatant)
        self.assertEqual(combatant["turn_start"], combatant["pos"])
        self.assertFalse(combatant["dashing"])

    def test_spawn_positions_sorted(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        goblin = srd.monsters()["goblin"]
        added = state_mod.spawn_monsters(room, [(goblin, 2)])
        self.assertEqual(len(added), 2)
        for entry in added:
            self.assertEqual(entry["kind"], "monster")
            self.assertEqual(entry["hp"], goblin["hp"])
            self.assertGreaterEqual(entry["pos"]["x"], 20.0)
        inits = [c["init"] for c in room.combat["combatants"]]
        self.assertEqual(inits, sorted(inits, reverse=True))

    def test_snapshot_syncs_pc_hp_from_character(self):
        """角色卡 HP/上限变化后，战斗板快照必须跟随（v0.6.33）。"""
        room, char = make_room()
        state_mod.start_combat(room)
        token = [c for c in room.combat["combatants"] if c["id"] == char["id"]][0]
        self.assertEqual((token["hp"], token["hp_max"]), (char["hp"]["cur"], char["hp"]["max"]))
        # 模拟升级/长休/法术/力竭/手动设置等直接改角色卡的路径
        char["hp"]["cur"] = 3
        char["hp"]["max"] = 25
        snap = room.snapshot()
        token = [c for c in snap["combat"]["combatants"] if c["id"] == char["id"]][0]
        self.assertEqual(token["hp"], 3)
        self.assertEqual(token["hp_max"], 25)

    def test_set_monster_hp_arbitrary_and_exact(self):
        """GM 怪物 HP：任意增量、精确设值、归零自动清理、找不到目标安全返回。"""
        room, _ = make_room()
        state_mod.start_combat(room)
        added = state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 1)])
        gid = added[0]["id"]
        found, pruned = state_mod.set_monster_hp(room, gid, delta=-3)
        self.assertTrue(found)
        self.assertEqual(pruned, [])
        self.assertEqual(added[0]["hp"], added[0]["hp_max"] - 3)
        found, _ = state_mod.set_monster_hp(room, gid, value=1)
        self.assertTrue(found)
        self.assertEqual(added[0]["hp"], 1)
        found, pruned = state_mod.set_monster_hp(room, gid, value=0)
        self.assertTrue(found)
        self.assertTrue(pruned)                      # 归零 → 清理播报
        found, _ = state_mod.set_monster_hp(room, "不存在", delta=1)
        self.assertFalse(found)

    def test_adjust_hp_character_and_monster(self):
        room, char = make_room()
        state_mod.start_combat(room)
        goblin = srd.monsters()["goblin"]
        state_mod.spawn_monsters(room, [(goblin, 1)])
        text = state_mod.adjust_hp(room, "测试勇者", -5)
        self.assertIn("受到 5 点伤害", text)
        self.assertEqual(char["hp"]["cur"], char["hp"]["max"] - 5)
        mirrored = [c for c in room.combat["combatants"] if c["id"] == "c1"][0]
        self.assertEqual(mirrored["hp"], char["hp"]["cur"])
        text = state_mod.adjust_hp(room, "地精", -99)
        self.assertIn("移出先攻条", text)
        self.assertIn("战斗结束", text)          # 唯一怪物倒地 → 全灭自动结束
        self.assertFalse(room.combat["active"])
        # 重新生成一只，验证伤害/治疗的常规路径
        state_mod.spawn_monsters(room, [(goblin, 1)])
        state_mod.adjust_hp(room, "地精", -2)
        state_mod.adjust_hp(room, "地精", 1)
        monster_row = [c for c in room.combat["combatants"] if c["kind"] == "monster"][0]
        self.assertEqual(monster_row["hp"], monster_row["hp_max"] - 1)

    def test_spawn_numbering(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        goblin = srd.monsters()["goblin"]
        added = state_mod.spawn_monsters(room, [(goblin, 2)])
        self.assertEqual([c["name"] for c in added], ["地精1", "地精2"])
        again = state_mod.spawn_monsters(room, [(goblin, 1)])
        self.assertEqual(again[0]["name"], "地精3")
        wolf = srd.monsters()["wolf"]
        single = state_mod.spawn_monsters(room, [(wolf, 1)])
        self.assertEqual(single[0]["name"], "狼")

    def test_later_batch_renumbers_existing(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        goblin = srd.monsters()["goblin"]
        first = state_mod.spawn_monsters(room, [(goblin, 1)])
        self.assertEqual(first[0]["name"], "地精")
        second = state_mod.spawn_monsters(room, [(goblin, 1)])
        self.assertEqual(first[0]["name"], "地精1")
        self.assertEqual(second[0]["name"], "地精2")

    def test_family_match_and_ambiguity(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        goblin = srd.monsters()["goblin"]
        state_mod.spawn_monsters(room, [(goblin, 2)])
        with self.assertRaises(ValueError) as ctx:
            state_mod.adjust_hp(room, "地精", -1)
        self.assertIn("编号", str(ctx.exception))
        text = state_mod.adjust_hp(room, "地精2", -2)
        self.assertIn("受到 2 点伤害", text)
        moved, summary = state_mod.move_combatant_toward(room, "地精", "测试勇者", 30)
        self.assertIsNotNone(moved)
        self.assertIn("移动", summary)

    def test_next_turn_ticks_effects(self):
        room, char = make_room()
        state_mod.start_combat(room)
        goblin = srd.monsters()["goblin"]
        state_mod.spawn_monsters(room, [(goblin, 1)])
        spells_mod.add_effect(char, "祝福术", 0.1, concentration=True)   # 1 轮
        first = state_mod.next_turn(room)
        self.assertEqual(first, [])
        second = state_mod.next_turn(room)    # 回到新一轮 → 时钟 +6 秒 → 效果到期
        self.assertTrue(any("祝福术" in text for text in second))
        self.assertIsNone(char.get("concentration"))

    def test_advance_clock_expires_effects(self):
        room, char = make_room()
        spells_mod.add_effect(char, "法师护甲", 10, clock_minutes=room.clock_minutes)
        notes = state_mod.advance_clock(room, 11)
        self.assertTrue(any("法师护甲" in text for text in notes))
        self.assertEqual(char["effects"], [])
        self.assertAlmostEqual(room.clock_minutes, 11.0)

    def test_long_rest_restores_and_clears(self):
        room, char = make_room()
        char["hp"]["cur"] = 2
        char["spell_slots"] = {"1": {"max": 2, "used": 2}}
        spells_mod.add_effect(char, "祝福术", 10, concentration=True)
        state_mod.long_rest(room)
        self.assertEqual(char["hp"]["cur"], char["hp"]["max"])
        self.assertEqual(char["spell_slots"]["1"]["used"], 0)
        self.assertEqual(char["effects"], [])
        self.assertIsNone(char["concentration"])
        self.assertGreaterEqual(room.clock_minutes, 480)

    def test_short_rest_advances_clock_only(self):
        """BFRD 短休：推进时钟并结算效果到期，不改法术位（奥术恢复由玩家手动处理）。"""
        room, char = make_room()
        char["class_id"] = "warlock"
        char["spell_slots"] = {"1": {"max": 1, "used": 1}}
        state_mod.short_rest(room)
        self.assertEqual(char["spell_slots"]["1"]["used"], 1)
        self.assertGreaterEqual(room.clock_minutes, 60)

    def test_effect_migration_from_rounds(self):
        room, char = make_room()
        char["effects"] = [{"name": "旧效果", "rounds": 3, "concentration": False, "note": ""}]
        room.migrate_effects()
        self.assertAlmostEqual(char["effects"][0]["expires_at"], 0.3, places=2)
        self.assertNotIn("rounds", char["effects"][0])

    def test_adjust_hp_runs_concentration_save(self):
        room, char = make_room()
        spells_mod.add_effect(char, "祝福术", 10, concentration=True)
        text = state_mod.adjust_hp(room, "测试勇者", -12)
        self.assertIn("专注检定", text)

    def test_companion_lifecycle(self):
        room, _ = make_room()
        companion = state_mod.create_companion(room, "小盾", "fighter", "human", 1)
        self.assertTrue(companion.get("companion"))
        state_mod.start_combat(room)
        allies = [c for c in room.combat["combatants"] if c.get("kind") == "ally"]
        self.assertEqual(len(allies), 1)
        self.assertEqual(allies[0]["name"], "小盾")
        state_mod.remove_companion(room, companion["id"])
        self.assertNotIn(companion["id"], room.characters)
        self.assertEqual([c for c in room.combat["combatants"] if c.get("kind") == "ally"], [])

    def test_companion_caster_scales_with_level(self):
        room, _ = make_room()
        companion = state_mod.create_companion(room, "小牧", "cleric", "dwarf", 2)
        self.assertEqual(companion["level"], 2)
        self.assertEqual(companion["spell_slots"]["1"]["max"], 3)
        self.assertTrue(companion["spells"])

    def test_room_meta_persists_and_reloads(self):
        room, _ = make_room()
        room.world = "世界文本"
        room.dm_note = "风格要求"
        room.campaign = campaign_mod.normalize_structure(
            {
                "title": "测试战役",
                "hub": {"name": "镇"},
                "pressure_type": "threat_clock",
                "pressure": {"label": "威胁", "max": 6, "level": 0},
                "quests": [
                    {"id": "q1", "tier": 1, "title": "A", "location": "L", "objective": "O", "completion": "C"}
                ],
                "finale": {"location": "F", "objective": "O"},
            }
        )
        # make_room 给实例打了 no-op 补丁，这里直接调用类上的真实实现
        state_mod.RoomState.persist_room_meta(room)
        fresh = RoomState()
        fresh.characters.clear()
        self.assertEqual(fresh.world, "世界文本")
        self.assertEqual(fresh.dm_note, "风格要求")
        self.assertEqual(fresh.campaign["title"], "测试战役")

    def test_milestone_hint_in_snapshot(self):
        room, char = make_room()
        structure = campaign_mod.normalize_structure(
            {
                "hub": {"name": "镇"},
                "pressure_type": "threat_clock",
                "pressure": {"label": "威胁", "max": 6, "level": 0},
                "quests": [
                    {"id": "q1", "tier": 1, "title": "A", "location": "L", "objective": "O", "completion": "C", "status": "completed"},
                    {"id": "q2", "tier": 1, "title": "B", "location": "L", "objective": "O", "completion": "C", "status": "completed"},
                    {"id": "q3", "tier": 1, "title": "C", "location": "L", "objective": "O", "completion": "C", "status": "completed"},
                ],
                "finale": {"location": "F", "objective": "O"},
            }
        )
        room.campaign = structure
        snapshot = room.snapshot()
        self.assertIn("milestone_hint", snapshot["campaign"])   # 3 个初始任务完成 → 应到 3 级
        char["level"] = 3
        snapshot = room.snapshot()
        self.assertNotIn("milestone_hint", snapshot["campaign"])

    def test_load_campaign_replaces_roster(self):
        room, _ = make_room()
        extra = state_mod.create_companion(room, "临时", "fighter")
        self.assertIn(extra["id"], room.characters)
        payload = {
            "format": "weavlum-campaign",
            "version": 1,
            "world": "",
            "summary": "",
            "ai_mode": True,
            "dm_sid": None,
            "combat": {"active": False, "round": 0, "turn": 0, "combatants": []},
            "characters": {"t1": {"id": "t1", "name": "存档角色", "hp": {"max": 10, "cur": 10}}},
            "log": [],
        }
        room.load_campaign(payload)
        # 完全替换：原角色与后加同伴都不在存档里 → 全被清掉
        self.assertNotIn("c1", room.characters)
        self.assertNotIn(extra["id"], room.characters)
        self.assertIn("t1", room.characters)

    def test_next_turn_persists_clock(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 1)])
        calls = []
        room.persist_room_meta = lambda: calls.append(room.clock_minutes)
        state_mod.next_turn(room)    # 未换轮：不落盘
        self.assertEqual(calls, [])
        state_mod.next_turn(room)    # 换轮：时钟 +0.1 并落盘
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0], 0.1)

    def test_short_rest_only_seated(self):
        room, char = make_room()
        char["class_id"] = "warlock"
        char["spell_slots"] = {"1": {"max": 1, "used": 1}}
        offline = {
            "id": "off1",
            "name": "离桌术士",
            "class_id": "warlock",
            "owner_sid": None,
            "spell_slots": {"1": {"max": 1, "used": 1}},
            "hp": {"max": 8, "cur": 8},
        }
        room.characters["off1"] = offline
        state_mod.short_rest(room)
        self.assertEqual(char["spell_slots"]["1"]["used"], 1)
        self.assertEqual(offline["spell_slots"]["1"]["used"], 1)

    def test_character_field_migration(self):
        char = {"name": "旧卡", "spell": {"ability": "int", "save_dc": 13, "attack_bonus": 5}}
        state_mod.migrate_character_fields(char)
        self.assertNotIn("spell", char)
        self.assertEqual(char["spellcasting"]["save_dc"], 13)

    def test_defeated_monster_pruned_and_combat_ends(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        added = state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 2)])
        text = state_mod.adjust_hp(room, added[0]["id"], -99)
        self.assertIn("移出先攻条", text)
        monsters = [c for c in room.combat["combatants"] if c["kind"] == "monster"]
        self.assertEqual(len(monsters), 1)
        self.assertTrue(room.combat["active"])
        text = state_mod.adjust_hp(room, added[1]["id"], -99)
        self.assertIn("战斗结束", text)
        self.assertFalse(room.combat["active"])
        self.assertEqual(room.combat["combatants"], [])

    def test_new_fight_resets_positions_after_wipe(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        added = state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 1)])
        state_mod.adjust_hp(room, added[0]["id"], -99)      # 全灭 → 自动结束
        self.assertFalse(room.combat["active"])
        state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 1)])
        self.assertTrue(room.combat["active"])
        pc_rows = [c for c in room.combat["combatants"] if c["kind"] == "pc"]
        self.assertEqual(len(pc_rows), 1)
        self.assertEqual(pc_rows[0]["pos"]["x"], 15.0)      # 站位已重新排列

    def test_reinforcement_does_not_reset_fight(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 1)])
        before = len(room.combat["combatants"])
        state_mod.spawn_monsters(room, [(srd.monsters()["wolf"], 1)])
        self.assertEqual(len(room.combat["combatants"]), before + 1)

    def test_end_combat_function(self):
        room, _ = make_room()
        self.assertEqual(state_mod.end_combat(room, "测试"), "当前不在战斗中")
        state_mod.start_combat(room)
        state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 1)])
        text = state_mod.end_combat(room, "玩家脱离")
        self.assertIn("玩家脱离", text)
        self.assertFalse(room.combat["active"])
        self.assertEqual(room.combat["combatants"], [])

    def test_briefing_text(self):
        room, _ = make_room()
        room.clock_minutes = 65
        text = state_mod.briefing_text(room)
        self.assertIn("第 1 天 01:05", text)
        self.assertIn("测试勇者", text)
        self.assertIn("冒险刚刚开始", text)
        # 日志为空但有剧情摘要时，简报回退用摘要
        room.summary = "此前队伍在泉眼清除了冰层下的异物。"
        text = state_mod.briefing_text(room)
        self.assertIn("剧情梗概", text)
        room.campaign = campaign_mod.normalize_structure(
            {
                "title": "测试战役",
                "hub": {"name": "镇"},
                "pressure_type": "threat_clock",
                "pressure": {"label": "威胁", "max": 6, "level": 1},
                "quests": [
                    {
                        "id": "q1", "tier": 1, "title": "矿洞失踪案", "location": "L",
                        "objective": "查明矿工失踪的真相", "completion": "C", "status": "in_progress",
                    },
                ],
                "finale": {"location": "F", "objective": "O"},
            }
        )
        state_mod.start_combat(room)
        state_mod.spawn_monsters(room, [(srd.monsters()["goblin"], 1)])
        room.add_log("narrate", "AI GM", "积雪的坡道上你们发现了脚印。")
        text = state_mod.briefing_text(room)
        self.assertIn("测试战役", text)
        self.assertIn("矿洞失踪案", text)
        self.assertIn("战斗进行中", text)
        self.assertIn("地精", text)
        self.assertIn("上次进展", text)

    def test_dm_assignment_flag_and_briefing(self):
        room, _ = make_room()
        room.ai_mode = True
        room.dm_sid = "s1"
        snapshot = room.snapshot()
        me_row = [p for p in snapshot["participants"] if p["sid"] == "s1"][0]
        self.assertTrue(me_row["is_dm"])     # 与主持模式无关：被指派即有 GM 位
        text = state_mod.briefing_text(room, "s1")
        self.assertIn("本场 GM", text)
        text = state_mod.briefing_text(room, "someone-else")
        self.assertNotIn("本场 GM", text)

    def test_move_requires_turn_and_clamps(self):
        room, _ = make_room()
        state_mod.start_combat(room)
        goblin = srd.monsters()["goblin"]
        added = state_mod.spawn_monsters(room, [(goblin, 1)])
        pc_row = [c for c in room.combat["combatants"] if c["id"] == "c1"][0]
        room.combat["combatants"] = [added[0], pc_row]
        room.combat["turn"] = 0
        with self.assertRaises(ValueError):
            state_mod.move_combatant_toward(room, "测试勇者", "地精", 30)
        room.combat["turn"] = 1
        moved, summary = state_mod.move_combatant_toward(room, "测试勇者", "地精", 999)
        self.assertIsNotNone(moved)
        self.assertIn("移动", summary)
        dist = state_mod.distance(moved["pos"], added[0]["pos"])
        self.assertGreaterEqual(dist, 4.99)


class MonsterBestiaryTests(unittest.TestCase):
    def test_all_monsters_have_core_stats(self):
        """图鉴完整性：全量 SRD 怪物都有核心字段（悬停图鉴与生成依赖）。"""
        db = srd.monsters()
        self.assertGreaterEqual(len(db), 300)
        for mid, m in db.items():
            self.assertTrue(m.get("name"), mid)
            self.assertIn("ac", m)
            self.assertIn("hp", m)
            self.assertIn("speed", m)
            self.assertIn("attack", m)
            self.assertIn("cr_label", m)
            self.assertIn("traits_zh", m)


if __name__ == "__main__":
    unittest.main()
