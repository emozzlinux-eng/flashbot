"""Low-level helpers: function selectors, minimal ABIs and a Multicall3 wrapper."""
import time

from eth_abi import decode
from web3 import Web3

ZERO = "0x0000000000000000000000000000000000000000"


def sel(signature: str) -> bytes:
    return bytes(Web3.keccak(text=signature)[:4])


SEL_ALL_PAIRS_LEN = sel("allPairsLength()")
SEL_ALL_PAIRS = sel("allPairs(uint256)")
SEL_GET_PAIR = sel("getPair(address,address)")
SEL_TOKEN0 = sel("token0()")
SEL_TOKEN1 = sel("token1()")
SEL_RESERVES = sel("getReserves()")
SEL_BALANCE_OF = sel("balanceOf(address)")
SEL_ROUTER_FACTORY = sel("factory()")
SEL_GET_AMOUNTS_OUT = sel("getAmountsOut(uint256,address[])")
SEL_DECIMALS = sel("decimals()")
SEL_SYMBOL = sel("symbol()")

MULTICALL3_ABI = [
    {
        "name": "aggregate3",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            }
        ],
    }
]

FLASHARB_ABI = [
    {
        "name": "execute",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenIn", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "routerBuy", "type": "address"},
            {"name": "routerSell", "type": "address"},
            {"name": "tokenMid", "type": "address"},
            {"name": "minProfit", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "owner",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]


def cs(address: str) -> str:
    return Web3.to_checksum_address(address)


def addr(raw: bytes) -> str:
    return cs(decode(["address"], raw)[0])


class Multi:
    """Batches many read-only calls into few RPC requests via Multicall3."""

    def __init__(self, w3, address: str, batch: int = 300, retries: int = 4):
        self.contract = w3.eth.contract(address=cs(address), abi=MULTICALL3_ABI)
        self.batch = batch
        self.retries = retries

    def call(self, calls, block=None):
        """calls: list of (target, calldata). Returns a list of bytes, or None where a call failed."""
        results = []
        for i in range(0, len(calls), self.batch):
            chunk = [(t, True, d) for t, d in calls[i : i + self.batch]]
            out = None
            for attempt in range(self.retries):
                try:
                    kwargs = {"block_identifier": block} if block is not None else {}
                    out = self.contract.functions.aggregate3(chunk).call(**kwargs)
                    break
                except Exception:
                    if attempt == self.retries - 1:
                        raise
                    time.sleep(1.5 * (attempt + 1))
            results.extend(bytes(data) if ok else None for ok, data in out)
        return results
