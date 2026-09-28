"""檔案存取：state、交易紀錄、賣出盤、CSV 紀錄、解析後的價量／法人。"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from .sources import QUOTE_FIELDS

CSV_COLUMNS = {
    "orders": ["date", "type", "order_id", "decision_date", "side", "code", "shares", "kind",
               "trade_id", "delay_sessions", "why"],
    "fills": ["date", "type", "side", "code", "shares", "open", "price", "cash_change", "amount",
              "order_id", "trade_id", "kind"],
    "daily": ["date", "cash", "positions_value", "equity", "n_positions", "gross",
              "bench_equity", "late_sessions",
              "shadow_pnl", "shadow_real_pnl", "shadow_n_early",
              "panel_ai_pnl", "panel_formula_pnl", "panel_n_early"],
    "risk_log": ["date", "item", "code", "result", "reason"],
    "shadow": ["date", "book", "type", "code", "trade_id", "decision", "reason", "shadow_stop",
               "side", "shares", "open", "price", "kind", "pnl", "why"],
    "sell_panel": ["date", "book", "type", "code", "trade_id", "decision", "reason",
                   "side", "shares", "open", "price", "kind", "order_id", "pnl", "why"],
    "runs": ["run_at", "session_date", "on_time", "new_buys_allowed", "blocked_reasons",
             "sessions_processed", "notes"],
}
EVENT_FILE = {"order_created": "orders", "order_cancelled": "orders", "order_carried": "orders",
              "exit_triggered": "orders", "corporate_action_suspect": "orders", "fill": "fills", "bench_fill": "fills",
              "dividend": "fills", "stock_dividend": "fills", "bench_dividend": "fills", "daily": "daily"}


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.ledger = self.root / "ledger"
        self.data = self.root / "data"

    # ---- paths
    @property
    def state_path(self) -> Path:
        return self.ledger / "state.json"

    def trade_path(self, tid: str) -> Path:
        return self.ledger / "trades" / f"{tid}.json"

    def proposal_path(self, d: dt.date) -> Path:
        return self.ledger / "proposals" / f"{d}.json"

    def briefing_path(self, d: dt.date) -> Path:
        return self.ledger / "briefings" / f"{d}.md"

    @property
    def raw_dir(self) -> Path:
        return self.data / "raw"

    # ---- json
    @staticmethod
    def _dump(path: Path, obj) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

    def load_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        self._dump(self.state_path, state)

    @property
    def panel_path(self) -> Path:
        return self.ledger / "panel.json"

    def load_panel(self) -> dict | None:
        return json.loads(self.panel_path.read_text(encoding="utf-8")) if self.panel_path.exists() else None

    def save_panel(self, panel: dict) -> None:
        self._dump(self.panel_path, panel)

    def load_trades(self) -> dict[str, dict]:
        d = self.ledger / "trades"
        if not d.exists():
            return {}
        return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("T*.json"))}

    def save_trades(self, trades: dict[str, dict]) -> None:
        for tid, t in trades.items():
            p = self.trade_path(tid)
            txt = json.dumps(t, ensure_ascii=False, indent=1)
            if not p.exists() or p.read_text(encoding="utf-8") != txt:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(txt, encoding="utf-8")

    # ---- csv
    def append(self, name: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = CSV_COLUMNS[name]
        p = self.ledger / f"{name}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)

    def append_events(self, events: list[dict]) -> None:
        by: dict[str, list] = {}
        for e in events:
            name = EVENT_FILE.get(e["type"])
            if name:
                by.setdefault(name, []).append(e)
        for name, rows in by.items():
            self.append(name, rows)

    def read_csv(self, name: str) -> list[dict]:
        p = self.ledger / f"{name}.csv"
        if not p.exists():
            return []
        with p.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    # ---- 解析後的市場資料
    def save_quotes(self, d: dt.date, quotes: dict) -> None:
        p = self.data / "prices" / f"{d}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=QUOTE_FIELDS)
            w.writeheader()
            for q in quotes.values():
                w.writerow(q)

    def load_quotes(self, d: dt.date) -> dict | None:
        p = self.data / "prices" / f"{d}.csv"
        if not p.exists():
            return None
        out = {}
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["code"]] = {k: (r[k] if k in ("code", "name") else
                                      (float(r[k]) if r[k] not in ("", "None") else None))
                                  for k in QUOTE_FIELDS}
        return out

    def price_dates(self) -> list[dt.date]:
        d = self.data / "prices"
        return sorted(dt.date.fromisoformat(p.stem) for p in d.glob("*.csv")) if d.exists() else []

    def save_institutional(self, d: dt.date, inst: dict) -> None:
        self._dump(self.data / "institutional" / f"{d}.json", inst)

    def load_institutional(self, d: dt.date) -> dict | None:
        p = self.data / "institutional" / f"{d}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_news(self, d: dt.date, news: dict) -> None:
        self._dump(self.data / "news" / f"{d}.json", news)

    def load_news(self, d: dt.date) -> dict:
        p = self.data / "news" / f"{d}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
