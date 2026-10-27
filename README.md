# Flash-loan arbitrage bot

Scans every Uniswap-V2-style DEX on a chain, finds tokens listed on two or more of them, works out the
best flash-loan size for each price gap, ranks routes by profit **after gas**, simulates the best one
against live state, and only then sends it. Loans come from Balancer (0% fee).

```
src/FlashArb.sol      contract: borrow -> swap on DEX A -> swap on DEX B -> repay -> sweep profit to owner
                      reverts unless profit >= minProfit, so a bad trade costs gas at most
bot/arb_math.py       exact V2 maths + closed-form optimal borrow size
bot/scanner.py        pool discovery (cached), Multicall reserve snapshots, opportunity ranking
bot/executor.py       eth_call / estimate_gas simulation, signing, sending
bot/main.py           CLI: verify | discover | scan | run | deploy
config.json           chains, DEXs, base tokens, thresholds
tests/                maths, offline pipeline, and an on-EVM contract test
```

## Quick start (about $5 of gas money is plenty)

```bash
cd flashbot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add BASE_RPC_URL (free Alchemy/Infura/QuickNode key)

python -m bot.main verify     # 1. checks every address + fee on-chain. Fix anything it flags.
python -m bot.main discover   # 2. builds the pair cache (first run is slow, then incremental)
python -m bot.main scan       # 3. logs profitable routes to opportunities.csv. No wallet, no money.
```

**Run step 3 for a day or two before spending anything.** If `opportunities.csv` stays empty, the
market has no free money for this strategy on that chain, and no amount of code changes that. Try
`--chain arbitrum` or `--chain polygon`, or add more DEXs.

When the scanner is showing real opportunities:

```bash
npm install && node scripts/compile.js          # -> build/FlashArb.json
# put PRIVATE_KEY (fresh wallet, ~$5 of gas) in .env
python -m bot.main deploy                        # prints FLASHARB_ADDRESS, add it to .env
python -m bot.main run                           # dry run: simulates the best route every block, sends nothing
python -m bot.main run --live                    # only after the dry run looks sane
```

## Safety controls

- `scan`, `verify`, `discover` are read-only. `run` without `--live` never sends a transaction.
- Every route is simulated with `eth_call` first; trades that would revert are never sent and are put on a 10-minute cooldown.
- The contract enforces `minProfit` on-chain (at least gas cost and half the expected profit).
- `max_failed_gas_loss_native` in `config.json` switches live sending off once failed transactions have burned that much gas.
- The key lives only in `.env` (gitignored). Use a throwaway wallet. The contract is `onlyOwner` and only the vault can call the callback.
- Never deploy or fund a contract you didn't write or fully read. "Ready-made flash loan bots" that ask you to send ETH to them are the standard scam.

## Know the limits

- Competing bots read the same pool states and pay builders for priority. Plain two-DEX gaps on big tokens are
  usually gone within the block. What remains tends to be long-tail tokens, where the risk of scam tokens is highest.
- Simulation protects you from reverts, not from being beaten: your transaction can land a block later, revert
  on-chain, and still cost gas. That is what the loss cap is for.
- L2 transactions also pay an L1 data fee. `extra_cost_native` is a rough allowance; adjust it from real receipts.
- "Every DEX and coin" here means every V2-style pair on the DEXs listed in `config.json`. Uniswap V3, Curve, and
  Balancer pools use different maths and are not included. Disabled DEXs in `config.json` need `verify` to confirm their fee first.
- Addresses in `config.json` come from memory of public deployments. `verify` checks them on-chain; trust it over the file.
- Check the tax rules where you live. This is not financial advice.

## Tests

```bash
python -m unittest discover -s tests -t . -v
# contract test on a local EVM (optional):
npm install && node scripts/compile_tests.js && pip install eth-tester py-evm
```
