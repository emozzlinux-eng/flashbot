"""Terminal dashboard for the flash-loan arb bot (Textual).

Run it with:

    python -m bot.tui [--config config.json] [--chain polygon] [--live]

Panels:
  * header   : chain / RPC / mode / latest block / gas price / cycle time
  * table    : ranked opportunities of the current snapshot (sorted by net profit)
  * log      : every scan cycle, simulation result, sent transaction, error
  * stats    : totals (cycles, opps seen, sims passed/sent, gas lost), session info

Keys:
  s        start / pause scanning
  l        toggle DRY-RUN <-> LIVE (LIVE needs FLASHARB_ADDRESS + PRIVATE_KEY)
  r        force one immediate cycle
  enter    simulate the selected opportunity (dry run, even while paused)
  t        cycle the minimum net-profit threshold through preset values
  q        quit

The TUI never invents its own trading logic: it reuses Engine.cycle() for
scanning and attempt() from bot.main for simulate/send, so behaviour matches
`python -m bot.main run`.
"""
import argparse
import logging
import os
import sys
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot.main import attempt, load_config, connect, make_engine  # noqa: E402
from bot.executor import Executor  # noqa: E402

NATIVE = 1e18


class LogBridge(logging.Handler):
    """Push the bot's own log records into the TUI log panel."""

    def __init__(self, app):
        super().__init__(level=logging.INFO)
        self.app = app
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.app.push_log(self.format(record), record.levelno)
        except Exception:
            pass


class StatusBar(Static):
    block = reactive("—")
    gas = reactive("—")
    cycle = reactive("—")

    def render(self) -> Text:
        mode = self.app.mode_label if hasattr(self.app, "mode_label") else ""
        t = Text()
        t.append(f" {self.app.chain_name} ", style="bold cyan")
        t.append(f"block {self.block}  gas {self.gas} gwei  cycle {self.cycle}  ", style="white")
        t.append(mode, style="bold red on dark_red" if self.app.live_enabled else "bold yellow")
        return t


class StatsPanel(Static):
    def watch_stats(self):
        self.refresh()

    def render(self) -> Text:
        a = self.app
        lines = [
            ("cycles", f"{a.cycles}"),
            ("opps seen", f"{a.opps_seen}"),
            ("sims passed", f"{a.sims_passed}"),
            ("sent ok", f"{a.sent_ok}"),
            ("reverted", f"{a.sent_bad}"),
            ("gas lost", f"{a.lost_wei / NATIVE:.6f} {a.native_symbol}"),
            ("threshold", f"{a.min_net / NATIVE:.6f} {a.native_symbol}"),
            ("cooldowns", f"{len(a.bad)} routes blocked"),
        ]
        t = Text(justify="left")
        for k, v in lines:
            t.append(f"{k:>12}: ", style="dim")
            t.append(f"{v}\n", style="bold green" if k == "sent ok" else
                     ("bold red" if k in ("reverted", "gas lost") and a.lost_wei > 0 else "white"))
        return t


class ArbTUI(App):
    CSS = """
    StatusBar { height: 1; background: $panel; }
    #main { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 2fr; }
    #table { height: 1fr; border: round $primary; }
    #logbox { height: 1fr; border: round $secondary; padding: 0 1; overflow-y: auto; }
    StatsPanel { height: auto; max-height: 12; border: round $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "toggle_scan", "Start/Pause"),
        ("l", "toggle_live", "Dry/Live"),
        ("r", "force_cycle", "Cycle now"),
        ("enter", "simulate_selected", "Simulate"),
        ("t", "cycle_threshold", "Threshold"),
    ]

    THRESHOLDS_FACTOR = [0.5, 1, 2, 5]  # multiples of config default

    def __init__(self, cfg_path, chain_override, live_cli):
        super().__init__()
        self.cfg = load_config(cfg_path, chain_override)
        self.native_symbol = self.cfg["native_symbol"]
        self.chain_name = self.cfg["name"]
        self.live_cli = live_cli
        self.engine = None
        self.executor = None
        self.w3 = None
        self.running = False
        self.force_next = False
        self.live_enabled = False
        self.bad = {}
        self.lost_wei = 0
        self.last_opps = []
        self.default_min_net = int(self.cfg["min_net_profit_native"] * NATIVE)
        self.min_net = self.default_min_net
        self.threshold_idx = 1
        # counters
        self.cycles = 0
        self.opps_seen = 0
        self.sims_passed = 0
        self.sent_ok = 0
        self.sent_bad = 0

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="table", cursor_type="row")
            with Vertical(id="right"):
                yield StatsPanel(id="stats")
                yield Static(id="logbox")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#table", DataTable)
        table.add_columns("route", "buy@dex", "sell@dex", "borrow", "gross", "gas", "net", "usd≈")
        table.focus()
        self.log_lines = []
        logging.getLogger().addHandler(LogBridge(self))
        logging.getLogger().setLevel(logging.INFO)
        self.set_interval(1.0, self._tick)
        self.push_log(f"config: chain={self.chain_name} dexes={len(self.cfg['dexes'])} "
                      f"poll={self.cfg['poll_seconds']}s", logging.INFO)
        self.run_worker(self._startup(), exclusive=True)

    # ------------------------------------------------------------- data plumbing
    @property
    def mode_label(self):
        if not self.running:
            return " PAUSED "
        return " LIVE - real transactions " if self.live_enabled else " DRY RUN (simulate only) "

    def push_log(self, msg, level=logging.INFO):
        box = self.query_one("#logbox", Static)
        color = {logging.WARNING: "yellow", logging.ERROR: "red", logging.CRITICAL: "bold red"}.get(level, "grey70")
        stamp = time.strftime("%H:%M:%S")
        self.log_lines.append((stamp, msg, color))
        self.log_lines = self.log_lines[-200:]
        text = Text(no_wrap=False)
        for s, m, c in self.log_lines[-40:]:
            text.append(f"[{s}] ", style="dim")
            text.append(m[:160] + "\n", style=c)
        box.update(text)

    def _tick(self):
        status = self.query_one(StatusBar)
        status.block = getattr(self, "_last_block", "—")
        status.gas = getattr(self, "_last_gas", "—")
        status.cycle = getattr(self, "_last_cycle_ms", "—")
        self.query_one(StatsPanel).refresh()

    async def _startup(self):
        try:
            self.w3 = connect(self.cfg)
            self.engine, _ = make_engine(self.cfg, self.w3)
            self.push_log("connecting… loading pool cache (first run may take a while)", logging.INFO)
            await self._run_in_thread(self.engine.load)
            contract = os.environ.get("FLASHARB_ADDRESS")
            if contract:
                key = os.environ.get("PRIVATE_KEY") if self.live_cli else None
                self.executor = Executor(self.w3, contract, self.cfg["chain_id"], key)
                self.push_log(f"FlashArb loaded at {contract}", logging.INFO)
            else:
                self.push_log("FLASHARB_ADDRESS not set — trade simulation disabled", logging.WARNING)
            self.running = True
            self.live_enabled = bool(self.live_cli and self.executor and self.executor.account)
            self.run_worker(self._loop(), exclusive=True)
        except SystemExit as exc:  # connect() calls sys.exit on bad RPC
            self.push_log(str(exc), logging.ERROR)
        except Exception as exc:
            self.push_log(f"startup failed: {exc}", logging.ERROR)

    def _run_in_thread(self, fn, *args):
        import anyio
        return anyio.to_thread.run_sync(lambda: fn(*args))

    # ------------------------------------------------------------- main loop
    async def _loop(self):
        while True:
            if not self.running and not self.force_next:
                await self._sleep_ui(0.5)
                continue
            self.force_next = False
            t0 = time.time()
            try:
                gas_price = await self._run_in_thread(lambda: self.w3.eth.gas_price)
                block, opps = await self._run_in_thread(self.engine.cycle, gas_price)
            except Exception as exc:
                self.push_log(f"cycle failed: {exc}", logging.WARNING)
                await self._sleep_ui(3)
                continue
            self.cycles += 1
            self._last_block = f"{block:,}"
            self._last_gas = f"{gas_price / 1e9:.2f}"
            self._last_cycle_ms = f"{(time.time() - t0) * 1000:.0f}ms"
            self._fill_table(opps)
            cap = int(self.cfg["max_failed_gas_loss_native"] * NATIVE)
            if opps:
                self.opps_seen += len(opps)
                self.push_log(f"block {block}: {len(opps)} profitable routes. Best: "
                              f"{self.engine.describe(opps[0])}", logging.INFO)
                if self.executor and self.running:
                    lost = await self._run_in_thread(attempt, self.engine, self.executor, opps,
                                                    self.cfg, gas_price, self.bad, self.live_enabled)
                    self.lost_wei += lost
                    if self.live_enabled and self.lost_wei > cap:
                        self.live_enabled = False
                        self.push_log(f"gas-loss cap hit ({self.lost_wei / NATIVE:.6f} "
                                      f"{self.native_symbol}); LIVE disabled, still simulating",
                                      logging.ERROR)
            else:
                self.push_log(f"block {block}: nothing above threshold", logging.INFO)
            await self._sleep_ui(max(0.0, self.cfg["poll_seconds"] - (time.time() - t0)))

    async def _sleep_ui(self, seconds):
        import anyio
        await anyio.sleep(seconds)

    def _fill_table(self, opps):
        if self.engine is None:
            return
        table = self.query_one("#table", DataTable)
        table.clear()
        self.last_opps = opps
        usd = self.engine.usd_per_native()
        for o in opps[:25]:
            sym = self.engine.base[o.token_in]
            dec = self.engine.base_dec[o.token_in]
            route = f"{sym}->{self.engine.symbol(o.token_mid)}->{sym}"
            usd_txt = f"${o.net_wei / NATIVE * usd:,.2f}" if usd else "—"
            style = "bold green" if o.net_wei >= self.min_net else "yellow"
            table.add_row(route, f"@{o.buy.dex.name}", f"@{o.sell.dex.name}",
                          f"{o.amount_in / 10 ** dec:,.2f}",
                          f"{o.gross_wei / NATIVE:.6f}", f"{o.gas_wei / NATIVE:.6f}",
                          Text(f"{o.net_wei / NATIVE:.6f}", style=style), usd_txt)
        if not opps:
            table.add_row("(none above threshold)", "", "", "", "", "", "", "")

    # ------------------------------------------------------------- actions
    def action_toggle_scan(self):
        self.running = not self.running
        self.push_log("scanning resumed" if self.running else "paused", logging.INFO)

    def action_toggle_live(self):
        if not self.executor:
            self.push_log("cannot go live: no FlashArb contract loaded", logging.WARNING)
            return
        if not self.live_enabled and self.executor.account is None:
            self.push_log("cannot go live: PRIVATE_KEY missing in .env", logging.WARNING)
            return
        self.live_enabled = not self.live_enabled
        self.push_log("LIVE MODE ON — sending real transactions" if self.live_enabled
                      else "switched back to dry run", logging.ERROR if self.live_enabled else logging.INFO)

    def action_force_cycle(self):
        self.force_next = True

    def action_cycle_threshold(self):
        self.threshold_idx = (self.threshold_idx + 1) % len(self.THRESHOLDS_FACTOR)
        self.min_net = int(self.default_min_net * self.THRESHOLDS_FACTOR[self.threshold_idx])
        self.cfg["min_net_profit_native"] = self.min_net / NATIVE
        self.push_log(f"min net profit threshold -> {self.min_net / NATIVE:.6f} {self.native_symbol}",
                      logging.INFO)
        self._fill_table(self.last_opps)

    async def action_simulate_selected(self):
        row = self.query_one("#table", DataTable).cursor_row
        if self.engine is None or row is None or not self.last_opps or not self.executor:
            self.push_log("nothing to simulate (select a row; needs FLASHARB_ADDRESS)", logging.WARNING)
            return
        opp = self.last_opps[min(row, len(self.last_opps) - 1)]
        min_profit = max(self.engine.oracle.from_wei(opp.token_in, opp.gas_wei),
                         int(opp.gross_profit * self.cfg["profit_safety_margin"]))
        ok, gas, err = await self._run_in_thread(self.executor.simulate, opp, min_profit)
        if ok:
            self.sims_passed += 1
            self.push_log(f"manual sim PASSED (gas {gas}): {self.engine.describe(opp)}", logging.INFO)
        else:
            self.push_log(f"manual sim reverted: {err}", logging.WARNING)

    def action_quit(self):
        self.exit()


def main():
    ap = argparse.ArgumentParser(description="Textual dashboard for the flash-arb bot")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--chain", help="override the chain set in config.json")
    ap.add_argument("--live", action="store_true", help="start with LIVE mode armed (still toggled by 'l')")
    args = ap.parse_args()
    ArbTUI(args.config, args.chain, args.live).run()


if __name__ == "__main__":
    main()
