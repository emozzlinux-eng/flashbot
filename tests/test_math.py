import random
import unittest

from bot.arb_math import get_amount_out, optimal_amount, round_trip


class TestArbMath(unittest.TestCase):
    def test_amount_out_matches_uniswap_formula(self):
        r_in, r_out, x = 1000 * 10**18, 2_000_000 * 10**6, 10**18
        expected = (x * 997 * r_out) // (r_in * 1000 + x * 997)
        self.assertEqual(get_amount_out(x, r_in, r_out, 30), expected)

    def test_zero_inputs(self):
        self.assertEqual(get_amount_out(0, 10, 10, 30), 0)
        self.assertEqual(get_amount_out(5, 0, 10, 30), 0)

    def test_aligned_pools_have_no_arbitrage(self):
        self.assertEqual(optimal_amount(10**6, 10**6, 30, 10**6, 10**6, 30, 10**9), (0, 0))

    def test_fees_can_wipe_out_a_small_gap(self):
        # 0.2% price gap is smaller than the 2 x 0.3% fees
        self.assertEqual(optimal_amount(10**9, 10**9, 30, 10**9, 998 * 10**6, 30, 10**12), (0, 0))

    def test_matches_brute_force(self):
        rng = random.Random(7)
        for _ in range(60):
            a1, b1, a2, b2 = (rng.randint(5_000, 12_000) for _ in range(4))
            f1, f2 = rng.choice([20, 25, 30]), rng.choice([20, 25, 30])
            brute = max(round_trip(x, a1, b1, f1, a2, b2, f2) - x for x in range(1, 3 * a1))
            x, got = optimal_amount(a1, b1, f1, a2, b2, f2, 10**12)
            self.assertGreaterEqual(got, brute - 2, (a1, b1, a2, b2, f1, f2))
            self.assertLessEqual(got, max(brute, 0), (a1, b1, a2, b2, f1, f2))
            if got:
                self.assertEqual(got, round_trip(x, a1, b1, f1, a2, b2, f2) - x)

    def test_respects_borrow_cap(self):
        x, p = optimal_amount(10**6, 10**6, 30, 10**6, 800_000, 30, 100)
        self.assertGreater(p, 0)
        self.assertLessEqual(x, 100)


if __name__ == "__main__":
    unittest.main()
