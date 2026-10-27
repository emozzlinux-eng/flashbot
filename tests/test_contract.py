"""Runs FlashArb on a local EVM (eth-tester) against mock tokens, a mock Balancer vault and mock routers.

Setup:  npm install && node scripts/compile_tests.js && pip install eth-tester py-evm
Skipped automatically if those aren't available.
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "build", "test_artifacts.json")

try:
    from eth_tester import EthereumTester, PyEVMBackend
    from web3 import EthereumTesterProvider, Web3
    HAVE_EVM = True
except Exception:  # pragma: no cover
    HAVE_EVM = False


@unittest.skipUnless(HAVE_EVM and os.path.exists(ART), "needs eth-tester + build/test_artifacts.json")
class TestFlashArbContract(unittest.TestCase):
    def setUp(self):
        with open(ART) as f:
            self.art = json.load(f)
        self.w3 = Web3(EthereumTesterProvider(EthereumTester(PyEVMBackend())))
        self.owner, self.other = self.w3.eth.accounts[0], self.w3.eth.accounts[1]

        self.a = self.deploy("MockERC20")      # borrowed token
        self.b = self.deploy("MockERC20")      # middle token
        self.vault = self.deploy("MockVault")
        self.buy = self.deploy("MockRouter", 2, 1)         # A -> B at 2.00
        self.sell = self.deploy("MockRouter", 55, 100)     # B -> A at 0.55  => 100 A -> 200 B -> 110 A
        self.arb = self.deploy("FlashArb", self.vault.address)

        self.tx(self.a.functions.mint(self.vault.address, 1000))
        self.tx(self.b.functions.mint(self.buy.address, 100_000))
        self.tx(self.a.functions.mint(self.sell.address, 100_000))

    def deploy(self, name, *args):
        art = self.art[name]
        c = self.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
        receipt = self.w3.eth.wait_for_transaction_receipt(c.constructor(*args).transact({"from": self.owner}))
        return self.w3.eth.contract(address=receipt["contractAddress"], abi=art["abi"])

    def tx(self, fn, sender=None):
        return self.w3.eth.wait_for_transaction_receipt(fn.transact({"from": sender or self.owner}))

    def run_arb(self, min_profit, sender=None, sell=None, amount=100):
        fn = self.arb.functions.execute(self.a.address, amount, self.buy.address,
                                        (sell or self.sell).address, self.b.address, min_profit)
        return self.tx(fn, sender)

    def test_profit_is_swept_to_owner_and_loan_repaid(self):
        self.run_arb(5)
        self.assertEqual(self.a.functions.balanceOf(self.owner).call(), 10)
        self.assertEqual(self.a.functions.balanceOf(self.vault.address).call(), 1000)
        self.assertEqual(self.a.functions.balanceOf(self.arb.address).call(), 0)

    def test_exact_min_profit_passes_and_one_more_reverts(self):
        self.run_arb(10)
        with self.assertRaises(Exception):
            self.run_arb(11)

    def test_losing_trade_reverts(self):
        bad_sell = self.deploy("MockRouter", 40, 100)      # 100 A -> 200 B -> 80 A
        self.tx(self.a.functions.mint(bad_sell.address, 100_000))
        with self.assertRaises(Exception):
            self.run_arb(0, sell=bad_sell)

    def test_only_owner_can_execute(self):
        with self.assertRaises(Exception):
            self.run_arb(0, sender=self.other)

    def test_callback_rejects_non_vault_callers(self):
        fn = self.arb.functions.receiveFlashLoan([self.a.address], [1], [0], b"")
        with self.assertRaises(Exception):
            self.tx(fn)


if __name__ == "__main__":
    unittest.main()
