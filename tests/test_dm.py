"""
GM 席位模型测试（自助接任 / 先到先得 / 占用拒绝 / 当 GM 放下角色 / 卸任 / 持久化）。

隔离方式与 test_combat 一致：角色目录与 room_meta 路径全部重定向到临时目录，
测试绝不触碰真实数据文件。
"""

import json
import tempfile
import unittest
from pathlib import Path

from server import characters
from server import state as state_mod
from server.state import RoomState

BASE = {"str": 15, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 8}


def make_room():
    """最小房间：s1（测试玩家，带角色 c1）、s2（路人玩家，无角色）。"""
    room = RoomState()
    room.characters.clear()
    room.participants.clear()
    room.persist_character = lambda char: None
    room.persist_room_meta = lambda: None
    room.seq = 1
    room.clock_minutes = 0.0
    room.ai_mode = True
    room.dm_sid = None
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
    room.participants["s2"] = {"nickname": "路人", "connected": True, "char_id": None, "is_host": False}
    return room, char


class DMSeatTests(unittest.TestCase):
    def setUp(self):
        """隔离：把角色目录与 meta 文件重定向到临时目录。"""
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_char_dir = state_mod.CHAR_DIR
        self._orig_meta_file = state_mod.ROOM_META_FILE
        state_mod.CHAR_DIR = Path(self._tmp.name) / "characters"
        state_mod.ROOM_META_FILE = Path(self._tmp.name) / "room_meta.json"

    def tearDown(self):
        state_mod.CHAR_DIR = self._orig_char_dir
        state_mod.ROOM_META_FILE = self._orig_meta_file
        self._tmp.cleanup()

    def test_claim_first_wins_and_idempotent(self):
        """先到先得：第一人成功；第二人被拒（提示占用者）；本人重复接任幂等成功。"""
        room, _ = make_room()
        ok, err = room.claim_dm("s2")
        self.assertTrue(ok)
        self.assertEqual(room.dm_sid, "s2")
        ok, err = room.claim_dm("s1")
        self.assertFalse(ok)
        self.assertIn("路人", err)
        self.assertEqual(room.dm_sid, "s2")
        ok, _ = room.claim_dm("s2")
        self.assertTrue(ok)
        self.assertEqual(room.dm_sid, "s2")

    def test_claim_requires_participant(self):
        """未登记昵称（不在参与者名单）不能接任。"""
        room, _ = make_room()
        ok, err = room.claim_dm("nobody")
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_claim_releases_character_and_combat(self):
        """当 GM 就不能有角色：接任时释放角色所有权并移出先攻条。"""
        room, char = make_room()
        state_mod.start_combat(room)
        self.assertTrue(any(c.get("id") == "c1" for c in room.combat["combatants"]))
        ok, _ = room.claim_dm("s1")
        self.assertTrue(ok)
        self.assertIsNone(char["owner_sid"])
        self.assertIsNone(char["owner_nick"])
        self.assertIsNone(room.participants["s1"]["char_id"])
        self.assertEqual(room.combat["combatants"], [])

    def test_release_only_by_holder(self):
        """卸任仅限当前 GM 本人。"""
        room, _ = make_room()
        room.claim_dm("s2")
        ok, _ = room.release_dm("s1")
        self.assertFalse(ok)
        self.assertEqual(room.dm_sid, "s2")
        ok, _ = room.release_dm("s2")
        self.assertTrue(ok)
        self.assertIsNone(room.dm_sid)

    def test_claim_does_not_touch_ai_gate(self):
        """总闸是"无 GM 时"的偏好：接任/卸任都不改总闸值。"""
        room, _ = make_room()
        room.ai_mode = True
        room.claim_dm("s2")
        self.assertTrue(room.ai_mode)
        room.release_dm("s2")
        self.assertTrue(room.ai_mode)

    def test_seat_persisted_in_room_meta(self):
        """席位随 room_meta 落盘：重启后仍归原 GM（其 cookie 不变即可续任）。"""
        room, _ = make_room()
        room.claim_dm("s2")
        state_mod.RoomState.persist_room_meta(room)
        data = json.loads(state_mod.ROOM_META_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["dm_sid"], "s2")
        fresh = RoomState()
        self.assertEqual(fresh.dm_sid, "s2")

    def test_briefing_presiding_lines(self):
        """简报按实际主持状态给提示：无人 / 本场 GM / 人类主持。"""
        room, _ = make_room()
        room.ai_mode = False
        text = state_mod.briefing_text(room)
        self.assertIn("无人主持", text)
        room.ai_mode = True
        text = state_mod.briefing_text(room)
        self.assertNotIn("无人主持", text)
        room.dm_sid = "s2"
        text = state_mod.briefing_text(room, "s2")
        self.assertIn("本场 GM", text)
        self.assertIn("人类 GM 主持", text)


if __name__ == "__main__":
    unittest.main()
