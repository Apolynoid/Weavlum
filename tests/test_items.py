"""
背包与货币系统测试（物品库 / 四级货币 / 交易 / 负重 / 旧卡迁移）。

隔离方式与 test_combat 一致：角色目录与 room_meta 路径全部重定向到临时目录，
测试绝不触碰真实数据文件。
"""

import tempfile
import unittest
from pathlib import Path

from server import actions, characters, srd
from server import state as state_mod
from tests.test_combat import make_room


class InventoryTests(unittest.TestCase):
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

    def test_items_db_shape(self):
        """物品库基本形状：条目数达标、全部有中文名。"""
        db = srd.items()
        self.assertGreaterEqual(len(db), 350)
        self.assertTrue(all(it.get("name") for it in db.values()))

    def test_character_created_with_items_and_coins(self):
        """建卡即带背包与四级货币；职业装备行以文本入包；背景腰包金币入袋。"""
        room, char = make_room()
        self.assertEqual(char["coins"], {"pp": 0, "gp": 10, "sp": 0, "cp": 0})   # 士兵背景腰包
        lines = [it for it in char["items"] if not it.get("id")]
        self.assertEqual(len(lines), 4)
        self.assertTrue(any("锁子甲" in it["name"] for it in lines))
        self.assertTrue(char["equipment"])

    def test_add_merge_and_remove(self):
        """入库（库 id）：同名合并数量；移除；不存在时报错。"""
        room, char = make_room()
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "torch", "qty": 2})
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "火把", "qty": 1})
        torches = [it for it in char["items"] if it.get("id") == "torch"]
        self.assertEqual(len(torches), 1)
        self.assertEqual(torches[0]["qty"], 3)
        actions.perform(room, "remove_item", {"character": "测试勇者", "item": "torch", "qty": 3})
        self.assertFalse([it for it in char["items"] if it.get("id") == "torch"])
        with self.assertRaises(ValueError):
            actions.perform(room, "remove_item", {"character": "测试勇者", "item": "不存在的东西"})

    def test_free_text_item(self):
        """自定义物品按自由文本入库（无 id）。"""
        room, char = make_room()
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "龙印拓片"})
        item = next(it for it in char["items"] if it["name"] == "龙印拓片")
        self.assertIsNone(item["id"])

    def test_item_notes(self):
        """自定义描述 + 库内物品备注：同备注合并、不同备注分开、可精确改/清。"""
        room, char = make_room()
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "上等治疗药水", "note": "品质：上等", "qty": 2})
        potions = [it for it in char["items"] if it["name"] == "上等治疗药水"]
        self.assertEqual(len(potions), 1)
        self.assertEqual(potions[0]["qty"], 2)
        self.assertEqual(potions[0]["note"], "品质：上等")
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "上等治疗药水", "note": "品质：稀释"})
        self.assertEqual(len([it for it in char["items"] if it["name"] == "上等治疗药水"]), 2)
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "上等治疗药水", "note": "品质：上等"})
        self.assertEqual(potions[0]["qty"], 3)
        actions.perform(room, "set_item_note", {
            "character": "测试勇者", "item": "上等治疗药水", "old_note": "品质：稀释", "note": "品质：浑浊"})
        self.assertTrue(any(it.get("note") == "品质：浑浊" for it in char["items"]))
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "potion-of-healing", "note": "劣质"})
        heal = next(it for it in char["items"] if it.get("id") == "potion-of-healing")
        self.assertEqual(heal["note"], "劣质")
        actions.perform(room, "set_item_note", {
            "character": "测试勇者", "item": "potion-of-healing", "old_note": "劣质", "note": "上等"})
        self.assertEqual(heal["note"], "上等")
        actions.perform(room, "set_item_note", {
            "character": "测试勇者", "item": "potion-of-healing", "old_note": "上等", "note": ""})
        self.assertNotIn("note", heal)

    def test_money_adjust_and_limits(self):
        """资产增减：自动按四级并档；设值；扣减超过持有报错。"""
        room, char = make_room()
        actions.perform(room, "adjust_gold", {"character": "测试勇者", "delta": 15.5})
        # 初始 10 金（士兵腰包）+ 15 金 5 银 → 分档保留：1 铂 15 金 5 银（总值 25.5 金）
        self.assertEqual(char["coins"], {"pp": 1, "gp": 15, "sp": 5, "cp": 0})
        with self.assertRaises(ValueError):
            actions.perform(room, "adjust_gold", {"character": "测试勇者", "delta": -100})
        actions.perform(room, "adjust_gold", {"character": "测试勇者", "value": 8.25})
        self.assertEqual(char["coins"], {"pp": 0, "gp": 8, "sp": 2, "cp": 5})

    def test_money_preserves_stacks(self):
        """分档保留：付款先花小额、必要时找开一枚大币并找零；收款只并入不动旧档。"""
        room, char = make_room()
        char["coins"] = {"pp": 0, "gp": 1, "sp": 13, "cp": 4}
        actions.perform(room, "adjust_gold", {"character": "测试勇者", "delta": -0.01})
        self.assertEqual(char["coins"], {"pp": 0, "gp": 1, "sp": 13, "cp": 3})
        actions.perform(room, "adjust_gold", {"character": "测试勇者", "delta": 0.02})
        self.assertEqual(char["coins"], {"pp": 0, "gp": 1, "sp": 13, "cp": 5})
        # 付 1.5 金：13 银花完 + 找开 1 金 → 找零 8 银 5 铜
        actions.perform(room, "adjust_gold", {"character": "测试勇者", "delta": -1.5})
        self.assertEqual(char["coins"], {"pp": 0, "gp": 0, "sp": 8, "cp": 5})

    def test_set_coins_action(self):
        """信任制四格设置：负数与非法值被规范化。"""
        room, char = make_room()
        actions.perform(room, "set_coins", {"character": "测试勇者", "coins": {"pp": 2, "gp": 0, "sp": 3, "cp": 9}})
        self.assertEqual(char["coins"], {"pp": 2, "gp": 0, "sp": 3, "cp": 9})
        actions.perform(room, "set_coins", {"character": "测试勇者", "coins": {"pp": -5, "gp": "7"}})
        self.assertEqual(char["coins"], {"pp": 0, "gp": 7, "sp": 0, "cp": 0})

    def test_exchange_currency(self):
        """四级整兑：10 进 1 出；非整除拒绝；余额不足拒绝。"""
        room, char = make_room()
        char["coins"] = {"pp": 0, "gp": 0, "sp": 0, "cp": 25}
        actions.perform(room, "exchange_currency", {"character": "测试勇者", "from": "cp", "to": "sp", "amount": 10})
        self.assertEqual(char["coins"]["sp"], 1)
        self.assertEqual(char["coins"]["cp"], 15)
        with self.assertRaises(ValueError):
            actions.perform(room, "exchange_currency", {"character": "测试勇者", "from": "cp", "to": "sp", "amount": 15})
        with self.assertRaises(ValueError):
            actions.perform(room, "exchange_currency", {"character": "测试勇者", "from": "pp", "to": "gp", "amount": 1})
        char["coins"] = {"pp": 1, "gp": 0, "sp": 0, "cp": 0}
        actions.perform(room, "exchange_currency", {"character": "测试勇者", "from": "pp", "to": "gp", "amount": 1})
        self.assertEqual(char["coins"]["gp"], 10)
        actions.perform(room, "exchange_currency", {"character": "测试勇者", "from": "gp", "to": "cp", "amount": 10})
        self.assertEqual(char["coins"]["gp"], 0)
        self.assertEqual(char["coins"]["cp"], 1000)      # 10 金 = 1000 铜

    def test_buy_and_sell(self):
        """购买扣钱入库（自动找零）；钱不够报错；出售半价回收；自定义物品不可卖。"""
        room, char = make_room()
        bag_cp = srd.items()["backpack"]["cost_cp"]      # 背包 2 金
        actions.perform(room, "adjust_gold", {"character": "测试勇者", "delta": 100})
        base_cp = characters.coin_total_cp(char["coins"])   # 初始 10 金 + 100 金
        actions.perform(room, "buy_item", {"character": "测试勇者", "item": "backpack", "qty": 2})
        self.assertEqual(characters.coin_total_cp(char["coins"]), base_cp - bag_cp * 2)
        with self.assertRaises(ValueError):
            actions.perform(room, "buy_item", {"character": "测试勇者", "item": "plate-armor", "qty": 1})
        actions.perform(room, "sell_item", {"character": "测试勇者", "item": "backpack", "qty": 1})
        self.assertEqual(characters.coin_total_cp(char["coins"]), base_cp - bag_cp * 2 + bag_cp // 2)
        actions.perform(room, "add_item", {"character": "测试勇者", "item": "龙印拓片"})
        with self.assertRaises(ValueError):
            actions.perform(room, "sell_item", {"character": "测试勇者", "item": "龙印拓片"})

    def test_get_inventory_text(self):
        """背包文本视图含资产（四级）、装备行与负重。"""
        room, _ = make_room()
        actions.perform(room, "adjust_gold", {"character": "测试勇者", "delta": 3})
        out = actions.perform(room, "get_inventory", {"character": "测试勇者"})
        self.assertIn("3 金币", out["text"])
        self.assertIn("锁子甲", out["text"])
        self.assertIn("负重", out["text"])
        self.assertIn("磅", out["text"])

    def test_migration_from_old_card(self):
        """老角色卡迁移：出身装备转背包；单一金币数值折算为四级并移除旧字段。"""
        old = {"id": "old1", "name": "老卡", "equipment": ["链甲", "火把"], "gold_gp": 1.5, "hp": {"max": 10, "cur": 10}}
        state_mod.migrate_character_fields(old)
        self.assertEqual(old["coins"], {"pp": 0, "gp": 1, "sp": 5, "cp": 0})
        self.assertNotIn("gold_gp", old)
        self.assertEqual(old.get("gender"), "")
        self.assertEqual(len(old["items"]), 2)
        torch = next(it for it in old["items"] if it["name"] == "火把")
        self.assertEqual(torch["id"], "torch")          # BFRD 库内名称可直接对上
        chain = next(it for it in old["items"] if it["name"] == "链甲")
        self.assertIsNone(chain["id"])                  # 旧译名（现为锁子甲）按自由文本保留

    def test_carry_info_tiers(self):
        """负重分级：正常 / 重度负重 / 超过上限（力量 16 → 上限 240、重度 160）。"""
        room, char = make_room()
        char["abilities"]["str"] = 16
        info = characters.carry_info(char)
        self.assertEqual(info["capacity"], 240)
        self.assertEqual(info["push"], 480)
        self.assertEqual(info["level"], "normal")       # 装备行未计重
        char["items"].append({"id": "chain-mail", "name": "锁子甲", "qty": 3})
        info = characters.carry_info(char)
        self.assertEqual(info["level"], "heavy")        # 55×3 = 165 > 160
        char["items"][-1]["qty"] = 5
        info = characters.carry_info(char)
        self.assertEqual(info["level"], "over")         # 55×5 = 275 > 240

    def test_carry_info_coin_weight_by_count(self):
        """硬币重量按枚数（50 枚/磅），与面值无关；自定义物品未计重并计数。"""
        room, char = make_room()
        char["items"].append({"id": None, "name": "护符", "qty": 1})
        char["coins"] = {"pp": 0, "gp": 0, "sp": 100, "cp": 0}   # 100 枚银币 = 2 磅
        info = characters.carry_info(char)
        self.assertAlmostEqual(info["weight"], 2.0, places=1)
        self.assertEqual(info["coin_count"], 100)
        self.assertEqual(info["unknown_count"], sum(1 for it in char["items"] if not it.get("id")))


    def test_reward_party(self):
        """全队发奖：每位上桌角色各得物品与金额；同伴不入列；空参数/无人在场报错。"""
        room, char = make_room()
        comp = state_mod.create_companion(room, "小弟", "fighter")
        out = actions.perform(room, "reward_party", {"item": "torch", "qty": 2, "gold": 5})
        self.assertTrue(out["changed"])
        torches = [it for it in char["items"] if it.get("id") == "torch"]
        self.assertEqual(torches[0]["qty"], 2)
        self.assertEqual(characters.coin_total_cp(char["coins"]), 1000 + 500)   # 初始 10 金 + 发奖 5 金
        self.assertFalse([it for it in comp["items"] if it.get("id") == "torch"])
        with self.assertRaises(ValueError):
            actions.perform(room, "reward_party", {})
        char["owner_sid"] = None
        with self.assertRaises(ValueError):
            actions.perform(room, "reward_party", {"gold": 1})


if __name__ == "__main__":
    unittest.main()
