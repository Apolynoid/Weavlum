import random
import unittest

from server import engine


class RollExprTests(unittest.TestCase):
    def test_flat_modifier(self):
        self.assertEqual(engine.roll_expr("5")["total"], 5)

    def test_d1_deterministic(self):
        self.assertEqual(engine.roll_expr("1d1+5")["total"], 6)
        self.assertEqual(engine.roll_expr("3d1")["total"], 3)

    def test_negative_dice(self):
        self.assertEqual(engine.roll_expr("-1d1+10")["total"], 9)

    def test_range(self):
        rng = random.Random(7)
        for _ in range(30):
            result = engine.roll_expr("2d6+3", rng)
            self.assertTrue(5 <= result["total"] <= 15)

    def test_normalization(self):
        self.assertEqual(engine.roll_expr(" 2D6 + 3 ")["expr"], "2d6+3")

    def test_invalid(self):
        for bad in ["", "abc", "1d", "d", "1d20++5", "2d6*2"]:
            with self.assertRaises(ValueError):
                engine.roll_expr(bad)


class RollCheckTests(unittest.TestCase):
    def test_normal(self):
        rng = random.Random(11)
        result = engine.roll_check(modifier=3, rng=rng)
        self.assertTrue(1 <= result["taken"] <= 20)
        self.assertEqual(result["total"], result["taken"] + 3)
        self.assertIsNone(result["success"])

    def test_advantage(self):
        rng = random.Random(3)
        mirror = random.Random(3)
        first, second = mirror.randint(1, 20), mirror.randint(1, 20)
        result = engine.roll_check(modifier=5, dc=15, mode="advantage", rng=rng)
        expected = max(first, second)
        self.assertEqual(result["taken"], expected)
        self.assertEqual(result["total"], expected + 5)
        self.assertEqual(result["success"], expected + 5 >= 15)

    def test_disadvantage(self):
        rng = random.Random(9)
        mirror = random.Random(9)
        first, second = mirror.randint(1, 20), mirror.randint(1, 20)
        result = engine.roll_check(mode="disadvantage", rng=rng)
        self.assertEqual(result["taken"], min(first, second))


class AbilityRollTests(unittest.TestCase):
    def test_4d6_drop_lowest(self):
        rng = random.Random(5)
        mirror = random.Random(5)
        results = engine.roll_ability_scores(rng)
        self.assertEqual(len(results), 6)
        for item in results:
            rolls = [mirror.randint(1, 6) for _ in range(4)]
            self.assertEqual(sorted(rolls), sorted(item["rolls"]))
            self.assertEqual(item["total"], sum(sorted(rolls, reverse=True)[:3]))


if __name__ == "__main__":
    unittest.main()
