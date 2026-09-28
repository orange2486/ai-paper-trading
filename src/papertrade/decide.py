"""讓一個全新、沒有任何工具的 Claude 讀當天簡報、寫提案（PLAN.md v1.2 第 9 節第 5、7 步）。

隔離：工作目錄是空的暫存資料夾（不載入專案 CLAUDE.md／記憶），`--tools ""`（不能讀檔、不能連網），
`--strict-mcp-config`（不載入 MCP）。AI 看得到的只有 DAILY_PROMPT.md、PROPOSAL_FORMAT.md 與當天簡報。
原始回應存在 ledger/ai/YYYY-MM-DD.json。

replay：試跑用，逐日重播 prepare → decide → finalize（不計成績；live 帳戶不准用）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import anon, config, daily
from .sources import Fetcher
from .store import Store

JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def build_prompt(repo: Path, briefing: str) -> str:
    return "\n\n".join([
        (repo / "DAILY_PROMPT.md").read_text(encoding="utf-8").strip(),
        "---\n\n" + (repo / "PROPOSAL_FORMAT.md").read_text(encoding="utf-8").strip(),
        "---\n\n" + briefing.strip(),
    ])


def extract_proposal(text: str) -> dict | None:
    blocks = JSON_BLOCK.findall(text)
    for b in reversed(blocks):
        try:
            obj = json.loads(b)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def run_claude(prompt: str, model: str | None = None, timeout: int = 1800) -> dict:
    exe = shutil.which("claude")
    if exe is None:
        raise SystemExit("找不到 claude CLI")
    cmd = [exe, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--setting-sources", "", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    with tempfile.TemporaryDirectory(prefix="papertrade_ai_") as sandbox:
        t0 = time.monotonic()
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           cwd=sandbox, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude 失敗（{r.returncode}）：{r.stderr[-2000:] or r.stdout[-2000:]}")
    out = json.loads(r.stdout)
    out["_wall_seconds"] = round(time.monotonic() - t0, 1)
    return out


def decide(store: Store, repo: Path, model: str | None = None) -> dict:
    state = store.load_state()
    day = state.get("day")
    if not day or day.get("status") != "awaiting_proposal":
        raise SystemExit("沒有等待提案的交易日（先跑 prepare）")
    session = dt.date.fromisoformat(day["date"])
    bp = store.briefing_path(session)
    if state.get("anon"):
        bp = bp.with_name(f"{session}.anon.md")
    prompt = build_prompt(repo, bp.read_text(encoding="utf-8"))
    out = run_claude(prompt, model)
    text = out.get("result", "")
    proposal = extract_proposal(text)
    raw_proposal = proposal
    if proposal is not None and state.get("anon"):
        proposal = anon.translate(proposal, state, store, session)
    rec = {"date": session.isoformat(), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
           "prompt_chars": len(prompt), "models": list((out.get("modelUsage") or {}).keys()),
           "cost_usd": out.get("total_cost_usd"), "seconds": out["_wall_seconds"],
           "usage": out.get("usage"), "parsed": proposal is not None, "result": text,
           "anon": bool(state.get("anon")), "proposal_as_written": raw_proposal}
    p = store.ledger / "ai" / f"{session}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    if proposal is not None:
        pp = store.proposal_path(session)
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text(json.dumps(proposal, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"session": session, "proposal": proposal, "record": p, "cost_usd": rec["cost_usd"],
            "seconds": rec["seconds"]}


def replay(store: Store, fx: Fetcher, repo: Path, start: dt.date, end: dt.date,
           model: str | None = None, log=print) -> None:
    d = start
    while d <= end:
        now = dt.datetime.combine(d, dt.time(20, 0), config.TZ)
        r = daily.prepare(store, fx, now)
        if r["session"] is None:
            d += dt.timedelta(days=1)
            continue
        try:
            dr = decide(store, repo, model)
            got = "有提案" if dr["proposal"] is not None else "回應解析失敗"
            log(f"{d} {got}｜{dr['seconds']} 秒｜${dr['cost_usd'] or 0:.2f}")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            log(f"{d} AI 失敗：{exc}（當天視為沒有提案）")
        f = daily.finalize(store)
        for row in f["log"]:
            if row["item"] in ("buy", "no_action", "proposal") or (row["result"] == "通過" and row["reason"].startswith("賣")):
                log(f"    [{row['result']}] {row['item']} {row['code']} {row['reason']}")
        d += dt.timedelta(days=1)
