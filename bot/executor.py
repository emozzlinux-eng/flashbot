"""Simulates and sends FlashArb.execute()."""
from eth_account import Account

from .chain import FLASHARB_ABI, cs


class Executor:
    def __init__(self, w3, contract_address, chain_id, private_key=None):
        self.w3 = w3
        self.chain_id = chain_id
        self.contract = w3.eth.contract(address=cs(contract_address), abi=FLASHARB_ABI)
        self.account = Account.from_key(private_key) if private_key else None
        self.owner = self.contract.functions.owner().call()

    def _fn(self, opp, min_profit):
        return self.contract.functions.execute(
            opp.token_in, opp.amount_in, opp.buy.dex.router, opp.sell.dex.router, opp.token_mid, min_profit
        )

    def simulate(self, opp, min_profit):
        """eth_call + estimate_gas against live state. Costs nothing. Returns (ok, gas, error)."""
        fn = self._fn(opp, min_profit)
        try:
            fn.call({"from": self.owner})
            gas = fn.estimate_gas({"from": self.owner})
            return True, gas, ""
        except Exception as exc:  # a revert here means the trade would have failed on-chain
            return False, 0, str(exc)[:160]

    def send(self, opp, min_profit, gas, gas_price_wei):
        if self.account is None:
            raise RuntimeError("no PRIVATE_KEY loaded")
        if self.account.address != self.owner:
            raise RuntimeError("PRIVATE_KEY is not the owner of the FlashArb contract")
        tx = self._fn(opp, min_profit).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "gas": int(gas * 1.3),
            "gasPrice": int(gas_price_wei * 1.1),
            "chainId": self.chain_id,
        })
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        return tx_hash, receipt
