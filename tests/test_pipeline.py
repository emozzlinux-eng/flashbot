"""End-to-end offline test: discovery -> reserve snapshot -> ranking, using a fake Multicall."""
import tempfile
import types
import unittest

from eth_abi import decode, encode
from web3 import Web3

from bot import chain as C
from bot.scanner import Engine


def A(n):
    return Web3.to_checksum_address("0x" + f"{n:040x}")


WETH, USDC, JUNK, OTHER = A(0x100), A(0x200), A(0x300), A(0x400)
VAULT, F1, F2, R1, R2 = A(0xBA), A(0xF1), A(0xF2), A(0xA1), A(0xA2)
P11, P12, P13, P21, P22 = A(0x1001), A(0x1002), A(0x1003), A(0x2001), A(0x2002)
E18, E6 = 10**18, 10**6


class FakeMulti:
    """Answers the same calls the real Multicall3 wrapper would, from an in-memory 'chain'."""

    def __init__(self, factories, pairs, balances):
        self.factories, self.pairs, self.balances = factories, pairs, balances

    def call(self, calls, block=None):
        out = []
        for target, data in calls:
            s, body = data[:4], data[4:]
            if s == C.SEL_ALL_PAIRS_LEN:
                out.append(encode(["uint256"], [len(self.factories[target])]))
            elif s == C.SEL_ALL_PAIRS:
                (i,) = decode(["uint256"], body)
                out.append(encode(["address"], [self.factories[target][i]]))
            elif s == C.SEL_TOKEN0:
                out.append(encode(["address"], [self.pairs[target][0]]))
            elif s == C.SEL_TOKEN1:
                out.append(encode(["address"], [self.pairs[target][1]]))
            elif s == C.SEL_RESERVES:
                out.append(encode(["uint112", "uint112", "uint32"], [self.pairs[target][2], self.pairs[target][3], 0]))
            elif s == C.SEL_BALANCE_OF:
                out.append(encode(["uint256"], [self.balances.get(target, 0)]))
            else:
                out.append(None)
        return out


def make(p21_reserves=(1000 * E18, 3_150_000 * E6), p22_reserves=(1_000_000 * E18, 110 * E18)):
    pairs = {
        P11: [WETH, USDC, 1000 * E18, 3_000_000 * E6],
        P12: [JUNK, WETH, 1_000_000 * E18, 100 * E18],
        P13: [JUNK, OTHER, 5 * E18, 5 * E18],          # no base token: must be ignored
        P21: [WETH, USDC, *p21_reserves],
        P22: [JUNK, WETH, *p22_reserves],
    }
    factories = {F1: [P11, P12, P13], F2: [P21, P22]}
    balances = {WETH: 10_000 * E18, USDC: 50_000_000 * E6}
    cfg = {
        "name": "test", "native_symbol": "ETH", "wrapped_native": WETH, "balancer_vault": VAULT,
        "gas_units_estimate": 380_000, "extra_cost_native": 0.0, "min_pool_liquidity_native": 1.0,
        "min_net_profit_native": 0.0001,
        "base_tokens": {"WETH": {"address": WETH, "decimals": 18},
                        "USDC": {"address": USDC, "decimals": 6, "usd": True}},
        "dexes": [{"name": "dex1", "factory": F1, "router": R1, "fee_bps": 30},
                  {"name": "dex2", "factory": F2, "router": R2, "fee_bps": 30}],
    }
    w3 = types.SimpleNamespace(eth=types.SimpleNamespace(block_number=123))
    eng = Engine(cfg, w3, FakeMulti(factories, pairs, balances), cache_dir=tempfile.mkdtemp())
    eng.load()
    return eng


class TestPipeline(unittest.TestCase):
    def test_discovery_keeps_only_base_pairs_listed_on_two_dexs(self):
        eng = make()
        self.assertEqual(len(eng.arb_groups), 2)
        self.assertEqual({p.address for p in eng.watch}, {P11, P12, P21, P22})

    def test_finds_and_ranks_opportunities(self):
        eng = make()
        block, opps = eng.cycle(gas_price_wei=10**9)
        self.assertEqual(block, 123)
        self.assertGreaterEqual(len(opps), 2)
        self.assertEqual([o.net_wei for o in opps], sorted((o.net_wei for o in opps), reverse=True))
        for o in opps:
            self.assertGreater(o.amount_in, 0)
            self.assertGreater(o.net_wei, 0)
            self.assertEqual(o.net_wei, o.gross_wei - o.gas_wei)
            self.assertIn(o.token_in, (WETH, USDC))

    def test_route_direction_is_correct(self):
        eng = make()
        _, opps = eng.cycle(10**9)
        junk = next(o for o in opps if o.token_mid == JUNK)
        # WETH buys more JUNK on dex1's pool, and JUNK buys more WETH on dex2's pool
        self.assertEqual((junk.token_in, junk.buy.address, junk.sell.address), (WETH, P12, P22))
        for o in (o for o in opps if {o.token_in, o.token_mid} == {WETH, USDC}):
            if o.token_in == WETH:      # sell WETH where USDC is richest (dex2), buy it back on dex1
                self.assertEqual((o.buy.address, o.sell.address), (P21, P11))
            else:
                self.assertEqual((o.buy.address, o.sell.address), (P11, P21))

    def test_aligned_markets_give_nothing(self):
        eng = make(p21_reserves=(1000 * E18, 3_000_000 * E6), p22_reserves=(1_000_000 * E18, 100 * E18))
        _, opps = eng.cycle(10**9)
        self.assertEqual(opps, [])

    def test_gas_can_kill_an_opportunity(self):
        eng = make()
        _, opps = eng.cycle(gas_price_wei=10**15)   # absurd gas price
        self.assertEqual(opps, [])

    def test_describe_does_not_crash(self):
        eng = make()
        _, opps = eng.cycle(10**9)
        text = eng.describe(opps[0])
        self.assertIn("net", text)
        self.assertIn("$", text)


if __name__ == "__main__":
    unittest.main()
