"""Command line entry point.

  python -m bot.main verify    check addresses, fees and Balancer liquidity on-chain (read-only)
  python -m bot.main discover  build the pair cache for every enabled DEX (read-only)
  python -m bot.main scan      look for opportunities and log them (read-only, no wallet needed)
  python -m bot.main run       scan + simulate the best route (dry run); add --live to really send it
  python -m bot.main deploy    deploy the FlashArb contract (needs PRIVATE_KEY)
"""
import argparse
import csv
import json
import logging
import os
import sys
import time

from eth_abi import decode, encode
from eth_account import Account
from web3 import Web3

from .arb_math import get_amount_out
from .chain import (
    SEL_BALANCE_OF, SEL_DECIMALS, SEL_GET_AMOUNTS_OUT, SEL_GET_PAIR, SEL_RESERVES,
    SEL_ROUTER_FACTORY, SEL_TOKEN0, ZERO, Multi, addr, cs,
)
from .executor import Executor
from .scanner import Engine

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger("flashbot")


def load_config(path, chain):
    with open(path) as f:
        raw = json.load(f)
    name = chain or raw["chain"]
    cfg = {**raw["settings"], **raw["chains"][name]}
    cfg["name"] = name
    return cfg


def connect(cfg):
    url = os.environ.get(cfg["rpc_env"]) or cfg["public_rpc"]
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        sys.exit(f"Cannot reach the RPC for {cfg['name']}. Set {cfg['rpc_env']} in .env")
    if w3.eth.chain_id != cfg["chain_id"]:
        sys.exit(f"RPC is on chain {w3.eth.chain_id}, expected {cfg['chain_id']}")
    return w3


def make_engine(cfg, w3):
    multi = Multi(w3, cfg["multicall3"])
    return Engine(cfg, w3, multi, cache_dir=os.path.join(ROOT, "cache")), multi


# --------------------------------------------------------------------------- verify
def cmd_verify(cfg, w3):
    eng, multi = make_engine(cfg, w3)
    problems = 0
    print(f"chain {cfg['name']} (id {w3.eth.chain_id}), native {cfg['native_symbol']}")
    if not w3.eth.get_code(cs(cfg["multicall3"])):
        print("!! no contract at the multicall3 address")
        problems += 1

    print("\nBase tokens (what the bot may flash-borrow):")
    for token, sym in eng.base.items():
        dec_raw, bal_raw = multi.call([(token, SEL_DECIMALS), (token, SEL_BALANCE_OF + encode(["address"], [eng.vault]))])
        dec = decode(["uint256"], dec_raw)[0] if dec_raw else None
        bal = decode(["uint256"], bal_raw)[0] if bal_raw else 0
        flag = ""
        if dec != eng.base_dec[token]:
            flag = "  !! decimals differ from config"
            problems += 1
        print(f"  {sym:7} decimals={dec}  balancer vault holds {bal / 10 ** (dec or 18):,.2f}{flag}")

    print("\nDEXs:")
    for d in eng.dexes:
        raw = multi.call([(d.router, SEL_ROUTER_FACTORY)])[0]
        rf = addr(raw) if raw else None
        if rf != d.factory:
            print(f"  {d.name}: !! router.factory() = {rf}, config says {d.factory}")
            problems += 1
            continue
        ref = None
        for token in eng.base:
            if token == eng.native:
                continue
            r = multi.call([(d.factory, SEL_GET_PAIR + encode(["address", "address"], [eng.native, token]))])[0]
            if r and addr(r) != ZERO:
                ref = (addr(r), token)
                break
        if not ref:
            print(f"  {d.name}: router/factory match, but no native/base reference pair to calibrate the fee")
            continue
        pair, token = ref
        t0_raw, res_raw = multi.call([(pair, SEL_TOKEN0), (pair, SEL_RESERVES)])
        r0, r1, _ = decode(["uint112", "uint112", "uint32"], res_raw)
        r_in, r_out = (r0, r1) if addr(t0_raw) == eng.native else (r1, r0)
        amount_in = max(r_in // 1000, 1)
        out_raw = multi.call([(d.router, SEL_GET_AMOUNTS_OUT + encode(["uint256", "address[]"], [amount_in, [eng.native, token]]))])[0]
        if not out_raw:
            print(f"  {d.name}: !! router.getAmountsOut failed")
            problems += 1
            continue
        out = decode(["uint256[]"], out_raw)[0][1]
        implied = [f for f in range(0, 101) if get_amount_out(amount_in, r_in, r_out, f) == out]
        ok = d.fee_bps in implied
        if not ok:
            problems += 1
        print(f"  {d.name}: router ok, fee in config = {d.fee_bps} bps, fee implied by router = {implied or 'non-standard'}"
              f"{'' if ok else '  !! fix fee_bps or leave this DEX disabled'}")
    print("\nAll checks passed." if not problems else f"\n{problems} problem(s) found - fix config.json before trading.")
    return 1 if problems else 0


# --------------------------------------------------------------------------- scan / run
def attempt(eng, ex, opps, cfg, gas_price, bad, live):
    """Simulate the best routes in order and send the first that passes. Returns gas wei lost on a failed tx."""
    extra = int(cfg["extra_cost_native"] * 1e18)
    min_net = int(cfg["min_net_profit_native"] * 1e18)
    for opp in opps[: cfg["top_n_to_simulate"]]:
        key = (opp.token_in, opp.token_mid, opp.buy.dex.name, opp.sell.dex.name)
        if bad.get(key, 0) > time.time():
            continue
        # on-chain guard: the tx reverts unless profit covers gas and at least half of what we expect
        min_profit = max(eng.oracle.from_wei(opp.token_in, opp.gas_wei),
                         int(opp.gross_profit * cfg["profit_safety_margin"]))
        ok, gas, err = ex.simulate(opp, min_profit)
        if not ok:
            bad[key] = time.time() + cfg["bad_route_cooldown_seconds"]
            log.info("  simulation reverted for %s via %s/%s: %s", eng.symbol(opp.token_mid),
                     opp.buy.dex.name, opp.sell.dex.name, err)
            continue
        if opp.gross_wei - (gas * gas_price + extra) < min_net:
            log.info("  simulation passed but real gas (%d) leaves too little profit", gas)
            continue
        if not live:
            log.info("  DRY RUN - simulation passed (gas %d). Would send: %s", gas, eng.describe(opp))
            return 0
        tx_hash, receipt = ex.send(opp, min_profit, gas, gas_price)
        if receipt["status"] == 1:
            log.info("  SENT ok: %s  (check your wallet for the profit)", tx_hash.hex())
            return 0
        lost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", gas_price)
        log.warning("  tx reverted on-chain, gas lost %.6f %s", lost / 1e18, cfg["native_symbol"])
        return lost
    return 0


def cmd_loop(cfg, w3, args, trade):
    eng, _ = make_engine(cfg, w3)
    eng.load()
    ex = None
    if trade:
        contract = os.environ.get("FLASHARB_ADDRESS")
        if not contract:
            sys.exit("Set FLASHARB_ADDRESS in .env (run `deploy` first)")
        key = os.environ.get("PRIVATE_KEY") if args.live else None
        if args.live and not key:
            sys.exit("--live needs PRIVATE_KEY in .env")
        ex = Executor(w3, contract, cfg["chain_id"], key)
    live = bool(trade and args.live)
    log.info("mode: %s", "LIVE - real transactions" if live else ("dry run (simulate only)" if trade else "scan only"))

    csv_path = os.path.join(ROOT, "opportunities.csv")
    new_file = not os.path.exists(csv_path)
    bad, lost_wei = {}, 0
    cap = int(cfg["max_failed_gas_loss_native"] * 1e18)
    with open(csv_path, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["time", "block", "route", "buy_dex", "sell_dex", "borrow_raw", "gross_native", "gas_native", "net_native"])
        while True:
            started = time.time()
            try:
                gas_price = w3.eth.gas_price
                block, opps = eng.cycle(gas_price)
            except Exception as exc:
                log.warning("cycle failed: %s", exc)
                time.sleep(3)
                continue
            if opps:
                log.info("block %d: %d profitable routes. Best: %s", block, len(opps), eng.describe(opps[0]))
                for o in opps[:20]:
                    writer.writerow([int(started), block, f"{eng.base[o.token_in]}->{o.token_mid}", o.buy.dex.name,
                                     o.sell.dex.name, o.amount_in, o.gross_wei / 1e18, o.gas_wei / 1e18, o.net_wei / 1e18])
                fh.flush()
                if ex:
                    lost_wei += attempt(eng, ex, opps, cfg, gas_price, bad, live)
                    if live and lost_wei > cap:
                        live = False
                        log.error("failed-transaction gas losses hit the cap (%.6f %s). Live sending is OFF; "
                                  "still simulating.", lost_wei / 1e18, cfg["native_symbol"])
            else:
                log.info("block %d: nothing above the profit threshold", block)
            if args.once:
                return 0
            time.sleep(max(0.0, cfg["poll_seconds"] - (time.time() - started)))


# --------------------------------------------------------------------------- deploy
def cmd_deploy(cfg, w3):
    key = os.environ.get("PRIVATE_KEY")
    if not key:
        sys.exit("Set PRIVATE_KEY in .env first (use a fresh wallet that only holds gas money)")
    art_path = os.path.join(ROOT, "build", "FlashArb.json")
    if not os.path.exists(art_path):
        sys.exit("build/FlashArb.json missing. Run:  npm install && node scripts/compile.js")
    with open(art_path) as f:
        art = json.load(f)
    acct = Account.from_key(key)
    fn = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]).constructor(cs(cfg["balancer_vault"]))
    gas = fn.estimate_gas({"from": acct.address})
    gas_price = w3.eth.gas_price
    print(f"deployer {acct.address}  balance {w3.eth.get_balance(acct.address) / 1e18:.6f} {cfg['native_symbol']}")
    print(f"estimated deploy cost {gas * gas_price / 1e18:.6f} {cfg['native_symbol']} on {cfg['name']}")
    if input("Type 'deploy' to continue: ").strip() != "deploy":
        return 0
    tx = fn.build_transaction({"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
                               "gas": int(gas * 1.2), "gasPrice": gas_price, "chainId": cfg["chain_id"]})
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=120)
    if receipt["status"] != 1:
        sys.exit("deployment failed")
    print(f"FlashArb deployed at {receipt['contractAddress']}\nAdd to .env:  FLASHARB_ADDRESS={receipt['contractAddress']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Flash-loan arbitrage scanner / executor")
    ap.add_argument("command", choices=["verify", "discover", "scan", "run", "deploy"])
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--chain", help="override the chain set in config.json")
    ap.add_argument("--once", action="store_true", help="one scan cycle, then exit")
    ap.add_argument("--live", action="store_true", help="with `run`: send real transactions")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    cfg = load_config(args.config, args.chain)
    w3 = connect(cfg)
    if args.command == "verify":
        return cmd_verify(cfg, w3)
    if args.command == "discover":
        eng, _ = make_engine(cfg, w3)
        eng.load()
        return 0
    if args.command == "deploy":
        return cmd_deploy(cfg, w3)
    return cmd_loop(cfg, w3, args, trade=(args.command == "run"))


if __name__ == "__main__":
    sys.exit(main())
