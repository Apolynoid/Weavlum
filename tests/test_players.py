"""
玩家档案测试（颜色调色板校验与自动分配、头像持久化、快照透传）。

隔离方式与 test_combat 一致：角色目录与 room_meta 路径全部重定向到临时目录。
"""

import tempfile
import unittest
from pathlib import Path

from server import state as state_mod
from server.state import PLAYER_COLORS, RoomState


def make_room():
    room = RoomState()
    room.characters.clear()
    room.participants.clear()
    room.player_profiles.clear()
    room.persist_character = lambda char: None
    room.persist_room_meta = lambda: None
    room.ai_mode = True
    room.dm_sid = None
    return room


class PlayerProfileTests(unittest.TestCase):
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

    def test_auto_color_and_distinct(self):
        """未选色自动分配；优先避开在场玩家已用的颜色。"""
        room = make_room()
        p1 = room.ensure_profile("甲")
        self.assertIn(p1["color"], PLAYER_COLORS)
        room.participants["s1"] = {"nickname": "甲", "color": p1["color"]}
        p2 = room.ensure_profile("乙")
        self.assertIn(p2["color"], PLAYER_COLORS)
        self.assertNotEqual(p1["color"], p2["color"])

    def test_explicit_color_and_invalid_fallback(self):
        """合法色生效；非法色（如注入字符串）被拒并保留旧值。"""
        room = make_room()
        p = room.ensure_profile("甲", color=PLAYER_COLORS[3])
        self.assertEqual(p["color"], PLAYER_COLORS[3])
        p = room.ensure_profile("甲", color="#123456")
        self.assertEqual(p["color"], PLAYER_COLORS[3])
        p2 = room.ensure_profile("乙", color="red; background:url(x)")
        self.assertIn(p2["color"], PLAYER_COLORS)

    def test_avatar_persists_roundtrip(self):
        """颜色与头像随 room_meta 落盘，重启恢复。"""
        room = make_room()
        room.ensure_profile("甲", avatar="abc123def456abcd.png")
        state_mod.RoomState.persist_room_meta(room)
        fresh = RoomState()
        self.assertEqual(fresh.player_profiles["甲"]["avatar"], "abc123def456abcd.png")
        self.assertIn(fresh.player_profiles["甲"]["color"], PLAYER_COLORS)

    def test_quick_rolls_persist_roundtrip(self):
        """固定掷骰按昵称存档，重启后仍在。"""
        room = make_room()
        room.ensure_profile("甲")
        saved = room.set_quick_rolls("甲", [
            {"label": "长剑", "expr": "1d20+5"},
            {"label": "火焰箭", "expr": "1d10"},
        ])
        self.assertEqual(saved, [
            {"label": "长剑", "expr": "1d20+5"},
            {"label": "火焰箭", "expr": "1d10"},
        ])
        state_mod.RoomState.persist_room_meta(room)
        fresh = RoomState()
        self.assertEqual(fresh.player_profiles["甲"]["quick_rolls"], saved)

    def test_quick_rolls_capped_at_eight(self):
        """状态层兜底：最多保存 8 条。"""
        room = make_room()
        saved = room.set_quick_rolls("甲", [{"label": str(i), "expr": "1d20"} for i in range(10)])
        self.assertEqual(len(saved), 8)

    def test_snapshot_participants_carry_profile(self):
        """快照中的参与者带颜色与头像（前端渲染用）。"""
        room = make_room()
        room.participants["s1"] = {
            "nickname": "甲", "connected": True, "char_id": None,
            "color": PLAYER_COLORS[1], "avatar": "x.png",
        }
        snap = room.snapshot()
        row = [p for p in snap["participants"] if p["sid"] == "s1"][0]
        self.assertEqual(row["color"], PLAYER_COLORS[1])
        self.assertEqual(row["avatar"], "x.png")


class RemoveCharacterTests(unittest.TestCase):
    """删除角色卡（本人/GM/主机权限由路由校验；此处测引擎清理语义）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_char_dir = state_mod.CHAR_DIR
        state_mod.CHAR_DIR = Path(self._tmp.name) / "characters"
        state_mod.CHAR_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        state_mod.CHAR_DIR = self._orig_char_dir
        self._tmp.cleanup()

    def _room_with_char(self):
        room = make_room()
        room.participants["s1"] = {"nickname": "甲", "char_id": "c1", "connected": True}
        room.participants["s2"] = {"nickname": "乙", "char_id": None, "connected": True}
        char = {"id": "c1", "name": "旧卡", "owner_sid": "s1", "companion": False,
                "hp": {"max": 10, "cur": 10}, "mods": {"dex": 0}, "speed": 30}
        room.characters["c1"] = char
        (state_mod.CHAR_DIR / "c1.json").write_text("{}", encoding="utf-8")
        return room, char

    def test_remove_character_cleans_everything(self):
        room, _ = self._room_with_char()
        room.combat = {
            "active": True, "round": 1, "turn": 1,
            "combatants": [
                {"id": "m1", "name": "哥布林", "kind": "monster", "init": 12},
                {"id": "c1", "name": "旧卡", "kind": "pc", "init": 10},
                {"id": "m2", "name": "哥布林2", "kind": "monster", "init": 8},
            ],
        }
        removed = state_mod.remove_character(room, "c1")
        self.assertEqual(removed["name"], "旧卡")
        self.assertNotIn("c1", room.characters)
        self.assertIsNone(room.participants["s1"]["char_id"])          # 解除会话绑定
        ids = [c["id"] for c in room.combat["combatants"]]
        self.assertNotIn("c1", ids)
        self.assertEqual(room.combat["turn"], 1)                        # 删除当前行动者：下一位滑到同索引，不跳过
        self.assertFalse((state_mod.CHAR_DIR / "c1.json").exists())     # 磁盘文件已删

    def test_remove_last_pc_ends_combat(self):
        room, _ = self._room_with_char()
        room.combat = {"active": True, "round": 1, "turn": 0,
                       "combatants": [{"id": "c1", "name": "旧卡", "kind": "pc", "init": 10}]}
        state_mod.remove_character(room, "c1")
        self.assertFalse(room.combat["active"])
        self.assertEqual(room.combat["combatants"], [])

    def test_remove_missing_raises(self):
        room = make_room()
        with self.assertRaises(ValueError):
            state_mod.remove_character(room, "nope")


class KickOfflinePlayerTests(unittest.TestCase):
    """GM 移出离线玩家：释放角色所有权、拒绝在线玩家与非 GM（v0.6.29）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_char_dir = state_mod.CHAR_DIR
        state_mod.CHAR_DIR = Path(self._tmp.name) / "characters"
        state_mod.CHAR_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        state_mod.CHAR_DIR = self._orig_char_dir
        self._tmp.cleanup()

    def _room(self):
        room = make_room()
        room.dm_sid = "gm"
        room.participants.update({
            "gm": {"nickname": "主持人", "connected": True, "char_id": None},
            "off": {"nickname": "掉线者", "connected": False, "char_id": "c1"},
            "on": {"nickname": "在线者", "connected": True, "char_id": None},
        })
        room.characters["c1"] = {
            "id": "c1", "name": "旧卡", "owner_sid": "off", "owner_nick": "掉线者",
        }
        return room

    def test_kick_offline_releases_character(self):
        room = self._room()
        ok, err, entry = room.kick_offline_player("off", "gm")
        self.assertTrue(ok, err)
        self.assertNotIn("off", room.participants)
        self.assertIsNone(room.characters["c1"]["owner_sid"])
        self.assertIsNone(room.characters["c1"]["owner_nick"])
        self.assertIn("掉线者", entry["text"])
        self.assertIn("旧卡", entry["text"])

    def test_reject_online_player(self):
        room = self._room()
        ok, err, entry = room.kick_offline_player("on", "gm")
        self.assertFalse(ok)
        self.assertIn("离线", err)
        self.assertIsNone(entry)
        self.assertIn("on", room.participants)

    def test_reject_non_gm_and_self(self):
        room = self._room()
        ok, err, _ = room.kick_offline_player("off", "on")
        self.assertFalse(ok)
        self.assertIn("GM", err)
        ok, err, _ = room.kick_offline_player("gm", "gm")
        self.assertFalse(ok)
        self.assertIn("自己", err)

    def test_reject_missing_target(self):
        room = self._room()
        ok, err, _ = room.kick_offline_player("nope", "gm")
        self.assertFalse(ok)
        self.assertIn("不在房间", err)


if __name__ == "__main__":
    unittest.main()
