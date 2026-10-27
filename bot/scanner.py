"""Pool discovery, reserve snapshots and opportunity ranking."""
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from itertools import permutations

from eth_abi import decode, encode

from .arb_math import optimal_amount
from .chain import (
    SEL_ALL_PAIRS,
    SEL_ALL_PAIRS_LEN,
    SEL_BALANCE_OF,
    SEL_RESERVES,
    SEL_SYMBOL,
    SEL_TOKEN0,
    SEL_TOKEN1,
    addr,
    cs,
)

log = logging.getLogger("scanner")


@dataclass(frozen=True)
class Dex:
    name: str
    factory: str
    router: str
    fee_bps: int


@dataclass
class Pool:
    dex: Dex
    address: str
    token0: str
    token1: str
    r0: int = 0
    r1: int = 0

    def reserves_for(self, token_in: str):
        """(reserve_in, reserve_out) when swapping token_in -> the other token."""
        return (self.r0, self.r1) if token_in == self.token0 else (self.r1, self.r0)


@dataclass
class Opp:
    token_in: str       # borrowed token (always a configured base token)
    token_mid: str
    amount_in: int      # borrow size, token_in units
    gross_profit: int   # token_in units, before gas
    buy: Pool           # pool where token_in -> token_mid
    sell: Pool          # pool where token_mid -> token_in
    gross_wei: int
    gas_wei: int
    net_wei: int


class Oracle:
    """Converts base-token amounts to native-currency wei using the deepest token/native pool."""

    def __init__(self, native: str):
        self.native = native
        self.rates = {native: (1, 1)}  # token -> (token reserve, native reserve)

    def update(self, pools):
        best = {}
        for p in pools:
            if p.r0 <= 0 or p.r1 <= 0:
                continue
            if p.token0 == self.native:
                other, r_n, r_t = p.token1, p.r0, p.r1
            else:
                other, r_n, r_t = p.token0, p.r1, p.r0
            if other not in best or r_n > best[other][1]:
                best[other] = (r_t, r_n)
        self.rates = {self.native: (1, 1), **best}

    def has(self, token):
        return token in self.rates

    def to_wei(self, token, amount):
        r_t, r_n = self.rates[token]
        return amount * r_n // r_t

    def from_wei(self, token, wei):
        r_t, r_n = self.rates[token]
        return wei * r_t // r_n


def discover_pools(multi, dex: Dex, base_tokens: set, cache_dir: str, max_pairs: int = 0, step: int = 2000):
    """Walk factory.allPairs() and keep every pair that contains a base token. Cached on disk."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"pairs_{dex.name}.json")
    base_hash = hashlib.sha1("".join(sorted(base_tokens)).encode()).hexdigest()
    cache = {"factory": dex.factory, "base": base_hash, "scanned": 0, "pairs": []}
    if os.path.exists(path):
        with open(path) as f:
            old = json.load(f)
        if old.get("factory") == dex.factory and old.get("base") == base_hash:
            cache = old

    raw = multi.call([(dex.factory, SEL_ALL_PAIRS_LEN)])[0]
    if raw is None:
        raise RuntimeError(f"{dex.name}: factory.allPairsLength() failed - check the factory address")
    total = decode(["uint256"], raw)[0]
    if max_pairs:
        total = min(total, max_pairs)

    i = cache["scanned"]
    while i < total:
        j = min(i + step, total)
        raw_addrs = multi.call([(dex.factory, SEL_ALL_PAIRS + encode(["uint256"], [k])) for k in range(i, j)])
        pairs = [addr(r) for r in raw_addrs if r]
        t0 = multi.call([(p, SEL_TOKEN0) for p in pairs])
        t1 = multi.call([(p, SEL_TOKEN1) for p in pairs])
        for p, a, b in zip(pairs, t0, t1):
            if not a or not b:
                continue
            x, y = addr(a), addr(b)
            if x in base_tokens or y in base_tokens:
                cache["pairs"].append([p, x, y])
        i = j
        cache["scanned"] = i
        with open(path, "w") as f:
            json.dump(cache, f)
        log.info("%s: scanned %d / %d pairs, kept %d", dex.name, i, total, len(cache["pairs"]))

    return [Pool(dex, p, x, y) for p, x, y in cache["pairs"]]


def group_pools(pools):
    """Group pools by token pair so the same market on different DEXs lines up."""
    groups = {}
    for p in pools:
        groups.setdefault(frozenset((p.token0, p.token1)), []).append(p)
    return groups


def refresh_reserves(multi, pools, block=None):
    res = multi.call([(p.address, SEL_RESERVES) for p in pools], block=block)
    for p, data in zip(pools, res):
        if data and len(data) >= 96:
            p.r0, p.r1, _ = decode(["uint112", "uint112", "uint32"], data)
        else:
            p.r0 = p.r1 = 0


def vault_balances(multi, vault, tokens, block=None):
    res = multi.call([(t, SEL_BALANCE_OF + encode(["address"], [vault])) for t in tokens], block=block)
    return {t: (decode(["uint256"], r)[0] if r else 0) for t, r in zip(tokens, res)}


def find_opportunities(groups, base_tokens, oracle, caps, gas_price_wei, gas_units,
                       extra_cost_wei, min_liq_wei, min_net_wei):
    """Every profitable (pool1 -> pool2) route, best net profit (after gas) first."""
    gas_wei = gas_units * gas_price_wei + extra_cost_wei
    out = []
    for key, pools in groups.items():
        live = [p for p in pools if p.r0 > 0 and p.r1 > 0]
        if len(live) < 2:
            continue
        for token_in in key:
            if token_in not in base_tokens or not oracle.has(token_in):
                continue
            token_mid = next(t for t in key if t != token_in)
            cap = caps.get(token_in, 0)
            if cap <= 0:
                continue
            for p1, p2 in permutations(live, 2):
                a1, b1 = p1.reserves_for(token_in)    # pool 1: token_in -> token_mid
                b2, a2 = p2.reserves_for(token_mid)   # pool 2: token_mid -> token_in
                if oracle.to_wei(token_in, min(a1, a2)) < min_liq_wei:
                    continue
                x, profit = optimal_amount(a1, b1, p1.dex.fee_bps, a2, b2, p2.dex.fee_bps, cap)
                if profit <= 0:
                    continue
                gross = oracle.to_wei(token_in, profit)
                net = gross - gas_wei
                if net < min_net_wei:
                    continue
                out.append(Opp(token_in, token_mid, x, profit, p1, p2, gross, gas_wei, net))
    out.sort(key=lambda o: o.net_wei, reverse=True)
    return out


class Engine:
    """Holds the watch list and runs one scan cycle at a time."""

    def __init__(self, cfg, w3, multi, cache_dir="cache"):
        self.cfg, self.w3, self.multi = cfg, w3, multi
        self.cache_dir = os.path.join(cache_dir, cfg["name"])
        self.dexes = [
            Dex(d["name"], cs(d["factory"]), cs(d["router"]), int(d["fee_bps"]))
            for d in cfg["dexes"]
            if d.get("enabled", True)
        ]
        self.base = {cs(v["address"]): k for k, v in cfg["base_tokens"].items()}      # address -> symbol
        self.base_dec = {cs(v["address"]): v["decimals"] for v in cfg["base_tokens"].values()}
        self.usd_tokens = {cs(v["address"]) for v in cfg["base_tokens"].values() if v.get("usd")}
        self.native = cs(cfg["wrapped_native"])
        self.vault = cs(cfg["balancer_vault"])
        self.oracle = Oracle(self.native)
        self.arb_groups, self.oracle_pools, self.watch = {}, [], []
        self._sym = {}

    def load(self):
        """Discover pools on every DEX (cached) and build the watch list."""
        pools = []
        for dex in self.dexes:
            pools += discover_pools(self.multi, dex, set(self.base), self.cache_dir,
                                    self.cfg.get("max_pairs_per_dex", 0))
        groups = group_pools(pools)
        self.arb_groups = {k: v for k, v in groups.items() if len(v) >= 2}
        base_others = set(self.base) - {self.native}
        self.oracle_pools = [p for k, v in groups.items() if self.native in k and (k - {self.native}) & base_others
                             for p in v]
        unique = {}
        for v in self.arb_groups.values():
            for p in v:
                unique[p.address] = p
        for p in self.oracle_pools:
            unique[p.address] = p
        self.watch = list(unique.values())
        log.info("%d pools total, %d markets listed on 2+ DEXs, watching %d pools",
                 len(pools), len(self.arb_groups), len(self.watch))

    def cycle(self, gas_price_wei):
        """Snapshot reserves at one block and return (block, ranked opportunities)."""
        block = self.w3.eth.block_number
        refresh_reserves(self.multi, self.watch, block)
        self.oracle.update(self.oracle_pools)
        caps = vault_balances(self.multi, self.vault, list(self.base), block)
        c = self.cfg
        opps = find_opportunities(
            self.arb_groups, set(self.base), self.oracle, caps, gas_price_wei,
            c["gas_units_estimate"], int(c["extra_cost_native"] * 1e18),
            int(c["min_pool_liquidity_native"] * 1e18), int(c["min_net_profit_native"] * 1e18),
        )
        return block, opps

    def symbol(self, token):
        if token in self.base:
            return self.base[token]
        if token not in self._sym:
            try:
                raw = self.multi.call([(token, SEL_SYMBOL)])[0]
                if raw and len(raw) > 32:
                    self._sym[token] = decode(["string"], raw)[0]
                elif raw:
                    self._sym[token] = raw[:32].rstrip(b"\0").decode(errors="ignore") or token[:8]
                else:
                    self._sym[token] = token[:8]
            except Exception:
                self._sym[token] = token[:8]
        return self._sym[token]

    def usd_per_native(self):
        best = None
        for t in self.usd_tokens:
            if t in self.oracle.rates:
                r_t, r_n = self.oracle.rates[t]
                if best is None or r_n > best[1]:
                    best = (r_t / 10 ** self.base_dec[t], r_n / 1e18)
        return best[0] / best[1] if best else None

    def describe(self, o: Opp):
        sym, dec = self.base[o.token_in], self.base_dec[o.token_in]
        native = self.cfg["native_symbol"]
        text = (f"{sym}->{self.symbol(o.token_mid)}->{sym}  buy@{o.buy.dex.name} sell@{o.sell.dex.name}  "
                f"borrow {o.amount_in / 10 ** dec:,.4f} {sym}  gross {o.gross_wei / 1e18:.6f}  "
                f"gas {o.gas_wei / 1e18:.6f}  net {o.net_wei / 1e18:.6f} {native}")
        usd = self.usd_per_native()
        if usd:
            text += f" (~${o.net_wei / 1e18 * usd:,.2f})"
        return text
