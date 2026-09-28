"""命令列。在 repo 根目錄執行：

    python -m papertrade init       --account trial --date 2026-10-05
    python -m papertrade bootstrap  --account trial --until 2026-10-02
    python -m papertrade prepare    --account trial            # 抓資料、結算、寫簡報
    python -m papertrade decide     --account trial            # 無工具的 Claude 讀簡報、寫提案
    python -m papertrade finalize   --account trial            # 風控 → 委託
    python -m papertrade replay     --account replay --from 2026-07-01 --to 2026-09-25   # 試跑：逐日重播
    python -m papertrade prepare    --account live --final     # 期末：全部下賣單

--now 只給試跑／測試用（例如重播過去某天）；正式帳戶不准用。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import config, daily, decide
from .sources import Fetcher
from .store import Store

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="papertrade")
    ap.add_argument("command", choices=["init", "bootstrap", "prepare", "decide", "finalize", "replay"])
    ap.add_argument("--account", default="trial", help="帳戶資料夾（trial＝試跑，live＝正式）")
    ap.add_argument("--date", help="init：初始化日（第一個決策日）")
    ap.add_argument("--until", help="bootstrap：抓到哪一天（含）")
    ap.add_argument("--sessions", type=int, default=70)
    ap.add_argument("--now", help="試跑用：假裝現在是這個台北時間，例如 2026-09-24T20:00")
    ap.add_argument("--final", action="store_true", help="期末：全部持股與對照組下賣單")
    ap.add_argument("--from", dest="start", help="replay：第一個重播日")
    ap.add_argument("--to", dest="end", help="replay：最後一個重播日")
    ap.add_argument("--model", help="decide／replay：指定模型（預設用 CLI 預設模型）")
    a = ap.parse_args(argv)

    store = Store(ROOT / a.account)
    fx = Fetcher(store.raw_dir)
    if (a.now or a.command == "replay") and a.account == "live":
        raise SystemExit("正式帳戶不准用 --now 或 replay")
    now = (dt.datetime.fromisoformat(a.now).replace(tzinfo=config.TZ) if a.now
           else dt.datetime.now(config.TZ))

    if a.command == "init":
        st = daily.init(store, dt.date.fromisoformat(a.date))
        print(f"已初始化 {store.root}：本金 {st['capital']:,}，初始化日 {st['init_date']}")
    elif a.command == "bootstrap":
        n = daily.bootstrap(store, fx, dt.date.fromisoformat(a.until), a.sessions)
        print(f"已抓 {n} 個交易日的歷史價量")
    elif a.command == "prepare":
        r = daily.prepare(store, fx, now, final=a.final)
        if r["session"] is None:
            print("本次沒有新的交易日：" + "；".join(r["notes"]))
            return
        print(f"交易日 {r['session']}｜可以新買：{'是' if r['new_buys_allowed'] else '否'}")
        for b in r["blocked"]:
            print(f"  不能新買：{b}")
        for n in r["notes"]:
            print(f"  註記：{n}")
        for e in r["events"]:
            if e["type"] in ("fill", "order_cancelled", "order_created", "exit_triggered", "dividend",
                             "bench_fill", "order_carried"):
                print(f"  {e['type']}: {e.get('side', '')} {e.get('code', '')} "
                      f"{e.get('shares', '')} {e.get('price', '')} {e.get('kind', '')} {e.get('why', '')}".rstrip())
        print(f"簡報：{r['briefing'].relative_to(ROOT)}")
        print(f"提案請寫到：{store.proposal_path(r['session']).relative_to(ROOT)}")
    elif a.command == "decide":
        r = decide.decide(store, ROOT, a.model)
        print(f"{r['session']}：{'已寫提案' if r['proposal'] is not None else '回應解析失敗，沒有提案'}"
              f"（{r['seconds']} 秒，${r['cost_usd'] or 0:.2f}）；原始回應 {r['record'].relative_to(ROOT)}")
    elif a.command == "replay":
        decide.replay(store, fx, ROOT, dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end),
                      a.model, log=lambda m: print(m, flush=True))
    elif a.command == "finalize":
        r = daily.finalize(store)
        for row in r["log"]:
            print(f"  [{row['result']}] {row['item']} {row['code']} {row['reason']}")


if __name__ == "__main__":
    main()
