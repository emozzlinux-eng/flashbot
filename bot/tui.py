"""Terminal dashboard for the flash-loan arb bot (Textual) — simple version.

Run it with:

    python -m bot.tui [--config config.json] [--chain polygon] [--live]

One big table of ranked opportunities + a one-line status bar + a compact
event feed. Everything you need is on screen or one keypress away:

  s        start / pause scanning
  l        toggle DRY-RUN <-> LIVE (LIVE needs FLASHARB_ADDRESS + PRIVATE_KEY)
  r        force one immediate scan cycle
  enter    simulate the selected opportunity (dry run, even while paused)
  f        filter routes by token symbol (type to match, enter to apply, esc clears)
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
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot.main import attempt, load_config, connect, make_engine  # noqa: E402
from bot.executor import Executor  # noqa: E402

NATIVE = 1e18


class LogBridge(logging.Handler):
    """Push the bot's own log records into the TUI event feed."""

    def __init__(self, app):
        super().__init__(level=logging.INFO)
        self.app = app
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.app.push_log(self.format(record), record.levelno)
        except Exception:
            pass


class FilterInput(Input):
    """Token filter box; Enter applies, Esc clears."""

    def on_key(self, event):
        if event.key == "enter":
            self.app.apply_filter(self.value)
            self.app.set_focus(self.app.query_one("#table", DataTable))
            event.stop()
            event.prevent_default()
        elif event.key == "escape":
            # clear + close the filter box (only when there is text to discard;
            # an empty Esc falls through to Textual's default blur behaviour)
            if self.value:
                self.app.apply_filter("")
                self.value = ""
                self.remove_class("show")
                self.app.set_focus(self.app.query_one("#table", DataTable))
                event.stop()
                event.prevent_default()


class ConfirmLive(ModalScreen[bool]):
    """Big, unmissable confirmation before enabling LIVE mode."""

    CSS = """
    ConfirmLive { align: center middle; }
    #box { width: 60; height: auto; padding: 1 2; border: thick red; background: $panel; }
    #box Label { margin: 0 1; }
    .yes { color: bold white on dark_red; }
    """

    def __init__(self, summary: str):
        super().__init__()
        self.summary = summary

    def compose(self) -> ComposeResult:
        yield Static(
            Text.from_markup(
                "[bold red]⚠  LIVE MODE  ⚠[/]\n\n"
                "This sends [bold]real transactions[/] with real funds.\n"
                "A reverted tx costs you gas.\n\n"
                f"[cyan]{self.summary}[/]\n\n"
                "Press [bold]y[/] to confirm, any other key to cancel."
            ),
            id="box",
        )

    def on_key(self, event):
        self.dismiss(event.key == "y")


class StatusBar(Static):
    block = reactive("—")
    gas = reactive("—")
    cycle = reactive("—")

    def render(self) -> Text:
        app = self.app
        t = Text()
        t.append(f" {app.chain_name} ", style="bold cyan")
        t.append(f"blk {self.block} · gas {self.gas} gwei · {self.cycle} · ", style="white")
        t.append(app.mode_label, style="bold white on dark_red" if app.live_enabled else "bold yellow")
        t.append(f"  sims {app.sims_passed} · sent {app.sent_ok}/{app.sent_bad} · "
                 f"lost {app.lost_wei / NATIVE:.5f} · thr {app.min_net / NATIVE:.6f} · "
                 f"{app.last_action}", style="dim")
        return t


class ArbTUI(App):
    TITLE = "FlashArb"

    CSS = """
    StatusBar { height: 1; background: $boost; }
    FilterInput { height: 1; display: none; }
    FilterInput.show { display: block; }
    #main { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 2fr; min-width: 42; }
    #table { height: 1fr; border: round $primary; }
    #feed { height: 1fr; border: round $secondary; padding: 0 1; overflow-y: auto; }
    #hint { height: 1; color: $text-muted; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "toggle_scan", "Start/Pause"),
        ("l", "toggle_live", "Dry/Live"),
        ("r", "force_cycle", "Scan now"),
        ("f", "show_filter", "Filter token"),
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
        self.all_opps = []          # every opp from the last cycle
        self.shown_opps = []        # after token filter, capped for the table
        self.token_filter = ""
        self.last_action = "—"
        self.default_min_net = int(self.cfg["min_net_profit_native"] * NATIVE)
        self.min_net = self.default_min_net
        self.threshold_idx = 1
        self.cycles = 0
        self.opps_seen = 0
        self.sims_passed = 0
        self.sent_ok = 0
        self.sent_bad = 0

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        yield FilterInput(placeholder="filter by token symbol… e.g. WETH", id="filter")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="table", cursor_type="row")
            with Vertical(id="right"):
                yield Static(id="feed")
                yield Static(id="hint")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#table", DataTable)
        table.add_columns("route", "buy → sell", "borrow", "gross", "gas", "net", "usd≈")
        table.focus()
        self.log_lines = []
        logging.getLogger().addHandler(LogBridge(self))
        logging.getLogger().setLevel(logging.INFO)
        self.set_interval(1.0, self._tick)
        self.query_one("#hint", Static).update(
            Text("↑↓ pick · enter sim · f filter · t threshold · s pause · r scan · l live", style="dim")
        )
        self.push_log(f"chain={self.chain_name}  dexes={len(self.cfg['dexes'])}  "
                      f"poll={self.cfg['poll_seconds']}s", logging.INFO)
        self.run_worker(self._startup(), exclusive=True)

    def on_key(self, event):
        # Esc while the filter box is shown: clear it and return to the table
        if event.key == "escape":
            box = self.query_one(FilterInput)
            if "show" in box.classes:
                box.value = ""
                self.apply_filter("")
                box.remove_class("show")
                self.set_focus(self.query_one("#table", DataTable))
                event.stop()
                event.prevent_default()

    # ------------------------------------------------------------- data plumbing
    @property
    def mode_label(self):
        if not self.running:
            return " PAUSED "
        return " LIVE — real transactions " if self.live_enabled else " DRY RUN "

    def push_log(self, msg, level=logging.INFO):
        box = self.query_one("#feed", Static)
        color = {logging.WARNING: "yellow", logging.ERROR: "red",
                 logging.CRITICAL: "bold red"}.get(level, "grey70")
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
        status.refresh()

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
            self.last_action = f"scan #{self.cycles}"
            self._fill_table(opps)
            cap = int(self.cfg["max_failed_gas_loss_native"] * NATIVE)
            if opps:
                self.opps_seen += len(opps)
                best = opps[0]
                self.push_log(f"block {block}: {len(opps)} routes above threshold. Best: "
                              f"{self.engine.symbol(best.token_mid)} via "
                              f"{best.buy.dex.name}/{best.sell.dex.name} "
                              f"net {best.net_wei / NATIVE:.6f} {self.native_symbol}",
                              logging.INFO)
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
        row0 = table.cursor_row
        table.clear()
        self.all_opps = opps
        flt = self.token_filter.strip().upper()
        shown = [o for o in opps if not flt or flt in self.engine.symbol(o.token_mid).upper()
                 or flt in self.engine.base[o.token_in].upper()] if flt else opps
        self.shown_opps = shown[:25]
        usd = self.engine.usd_per_native()
        for o in self.shown_opps:
            sym = self.engine.base[o.token_in]
            dec = self.engine.base_dec[o.token_in]
            route = f"{sym}->{self.engine.symbol(o.token_mid)}->{sym}"
            usd_txt = f"${o.net_wei / NATIVE * usd:,.2f}" if usd else "—"
            style = "bold green" if o.net_wei >= self.min_net else "yellow"
            table.add_row(route, f"{o.buy.dex.name} → {o.sell.dex.name}",
                          f"{o.amount_in / 10 ** dec:,.2f}",
                          f"{o.gross_wei / NATIVE:.6f}", f"{o.gas_wei / NATIVE:.6f}",
                          Text(f"{o.net_wei / NATIVE:.6f}", style=style), usd_txt)
        if not self.shown_opps:
            msg = "(none above threshold)" if not flt else f"(no routes matching '{flt}')"
            table.add_row(msg, "", "", "", "", "", "")
        if row0 is not None and self.shown_opps:
            table.move_cursor(row=min(row0, len(self.shown_opps) - 1))

    # ------------------------------------------------------------- actions
    def action_toggle_scan(self):
        self.running = not self.running
        self.last_action = "resumed" if self.running else "PAUSED"
        self.push_log("scanning resumed" if self.running else "paused", logging.INFO)

    def action_show_filter(self):
        box = self.query_one(FilterInput)
        box.add_class("show")
        box.focus()

    def apply_filter(self, value):
        self.token_filter = value
        self.last_action = f"filter={value.strip().upper() or 'off'}"
        self._fill_table(self.all_opps)

    def action_toggle_live(self):
        if not self.executor:
            self.push_log("cannot go live: no FlashArb contract loaded", logging.WARNING)
            return
        if not self.live_enabled and self.executor.account is None:
            self.push_log("cannot go live: PRIVATE_KEY missing in .env", logging.WARNING)
            return
        if self.live_enabled:
            self.live_enabled = False
            self.last_action = "DRY RUN"
            self.push_log("switched back to dry run", logging.INFO)
            return
        summary = (f"threshold {self.min_net / NATIVE:.6f} {self.native_symbol} · "
                   f"top {self.cfg['top_n_to_simulate']} routes/cycle · "
                   f"gas lost so far {self.lost_wei / NATIVE:.6f}")
        self.push_log("confirm LIVE mode? (y = yes)", logging.WARNING)

        def after(confirmed):
            if confirmed:
                self.live_enabled = True
                self.last_action = "LIVE ON"
                self.push_log("LIVE MODE ON — sending real transactions", logging.ERROR)

        self.push_screen(ConfirmLive(summary), after)

    def action_force_cycle(self):
        self.force_next = True

    def action_cycle_threshold(self):
        self.threshold_idx = (self.threshold_idx + 1) % len(self.THRESHOLDS_FACTOR)
        self.min_net = int(self.default_min_net * self.THRESHOLDS_FACTOR[self.threshold_idx])
        self.cfg["min_net_profit_native"] = self.min_net / NATIVE
        self.last_action = f"thr {self.min_net / NATIVE:.6f}"
        self.push_log(f"min net profit threshold -> {self.min_net / NATIVE:.6f} {self.native_symbol}",
                      logging.INFO)
        self._fill_table(self.all_opps)

    async def action_simulate_selected(self):
        table = self.query_one("#table", DataTable)
        row = table.cursor_row
        if self.engine is None or row is None or not self.shown_opps or not self.executor:
            self.push_log("nothing to simulate (select a row; needs FLASHARB_ADDRESS)", logging.WARNING)
            return
        opp = self.shown_opps[min(row, len(self.shown_opps) - 1)]
        self.last_action = f"sim {self.engine.symbol(opp.token_mid)}"
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
