#!/usr/bin/env python3
"""
Standalone dashboard builder for the options screener.

Reads every data/<date>/analysis_<date>.json + shortlist_<date>.json + data_agent_<date>.json
already written by `orchestrator.py run`, and produces:

  data/run_log.csv      one row per day: completion time (UTC), tickers run, flags found, caveats
  data/ticker_log.csv   one row per ticker per day: volume, C/P ratio, z-score, OI status, price move
  data/dashboard.md     human-readable summary: run history + latest-run table + per-ticker trend

Does not call Yahoo or Claude and does not modify orchestrator.py. Safe to re-run any time;
it fully rebuilds the CSVs/dashboard from whatever is on disk (idempotent per run_date).

Usage
    python orchestrator.py run --watchlist watchlist.txt   # produces data/<date>/*.json
    python dashboard.py                                     # rebuild run_log.csv / ticker_log.csv / dashboard.md
    python dashboard.py --data-dir data --lookback 20
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PT = ZoneInfo("America/Los_Angeles")  # displays as PST or PDT depending on the date


def to_pt_display(utc_iso: str) -> str:
    """Convert a stored UTC ISO timestamp to a human-readable Pacific-time string.
    The underlying CSV/JSON keep UTC (portable, unambiguous); only display converts to PT."""
    if not utc_iso:
        return ""
    try:
        dt = datetime.fromisoformat(utc_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        pt = dt.astimezone(PT)
        return pt.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except (ValueError, TypeError):
        return utc_iso

RUN_LOG_COLS = ["run_date", "completed_at_utc", "tickers", "ticker_count",
                "flags_count", "flagged_tickers", "data_mode", "oi_missing_policy", "run_caveats"]

TICKER_LOG_COLS = ["run_date", "ticker", "options_volume_total", "calls", "puts", "cp_ratio",
                    "options_baseline_status", "zscore", "ratio_to_median", "oi_classification",
                    "price_last_close", "price_chg_1d_pct", "price_move_label", "price_vs_options",
                    "sector", "sector_wide", "event_could_explain", "flagged"]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iter_run_dirs(data_dir: Path):
    """Yield (run_date, dir_path) for every dated run folder, oldest first."""
    if not data_dir.exists():
        return
    for d in sorted(data_dir.iterdir()):
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name):
            yield d.name, d


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def build_logs(data_dir: Path) -> tuple[list[dict], list[dict]]:
    """Scan every data/<date>/ folder and rebuild run_log / ticker_log rows from scratch."""
    run_rows: list[dict] = []
    ticker_rows: list[dict] = []

    for run_date, d in iter_run_dirs(data_dir):
        a = load_json(d / f"analysis_{run_date}.json")
        sl = load_json(d / f"shortlist_{run_date}.json")
        dat = load_json(d / f"data_agent_{run_date}.json")
        if not a:
            continue  # incomplete run; skip rather than guess

        tickers = a.get("tickers", [])
        entry_by_ticker = {it["ticker"]: it for it in tickers if isinstance(it, dict)}
        ticker_list = list(entry_by_ticker) or [t for t in tickers if isinstance(t, str)]

        flagged = [it["ticker"] for it in (sl or {}).get("items", [])]
        completed_at = (sl or {}).get("generated_at_utc") or ""

        run_rows.append({
            "run_date": run_date,
            "completed_at_utc": completed_at,
            "tickers": " ".join(ticker_list),
            "ticker_count": len(ticker_list),
            "flags_count": len(flagged),
            "flagged_tickers": " ".join(flagged) or "none",
            "data_mode": (dat or {}).get("data_mode", ""),
            "oi_missing_policy": a.get("oi_missing_policy", ""),
            "run_caveats": " | ".join(a.get("run_caveats", []))[:500],
        })

        flagged_set = set(flagged)
        for t, m in a.get("metrics", {}).items():
            ot = m.get("options_today", {}) or {}
            ob = m.get("options_baseline", {}) or {}
            oi = m.get("oi", {}) or {}
            price = m.get("price", {}) or {}
            pvo = m.get("price_vs_options", {}) or {}
            sc = m.get("sector_context", {}) or {}
            entry = entry_by_ticker.get(t, {})
            sched = entry.get("scheduled_events")
            ticker_rows.append({
                "run_date": run_date, "ticker": t,
                "options_volume_total": ot.get("total"), "calls": ot.get("calls"), "puts": ot.get("puts"),
                "cp_ratio": ot.get("cp_ratio"),
                "options_baseline_status": ob.get("status"), "zscore": ob.get("zscore"),
                "ratio_to_median": ob.get("ratio_to_median"),
                "oi_classification": oi.get("classification"),
                "price_last_close": price.get("last_close"), "price_chg_1d_pct": price.get("chg_1d_pct"),
                "price_move_label": price.get("move_label"), "price_vs_options": pvo.get("relationship"),
                "sector": sc.get("sector"), "sector_wide": sc.get("sector_wide"),
                "event_could_explain": sched.get("could_explain") if isinstance(sched, dict) else None,
                "flagged": t in flagged_set,
            })

    run_rows.sort(key=lambda r: r["run_date"])
    ticker_rows.sort(key=lambda r: (r["run_date"], r["ticker"]))
    return run_rows, ticker_rows


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def render_dashboard(run_rows: list[dict], ticker_rows: list[dict], lookback: int) -> str:
    L = ["# Options screener — daily dashboard", "",
         f"_Last rebuilt: {to_pt_display(utcnow_iso())}. Built from data/<date>/analysis_*.json + "
         f"shortlist_*.json. Descriptive only, not a recommendation._", ""]

    if not run_rows:
        L.append("No completed runs found under `data/`. Run `python orchestrator.py run ...` first, "
                  "then `python dashboard.py`.")
        return "\n".join(L) + "\n"

    L += ["## Run history", "",
          "| Date | Completed (PT) | Tickers run | Flags | Flagged tickers |",
          "|---|---|---|---|---|"]
    for r in run_rows[-lookback:]:
        L.append(f"| {r['run_date']} | {to_pt_display(r['completed_at_utc']) or '—'} | {r['ticker_count']} "
                  f"| {r['flags_count']} | {r['flagged_tickers']} |")
    L.append("")

    latest_date = run_rows[-1]["run_date"]
    latest = [r for r in ticker_rows if r["run_date"] == latest_date]
    L += [f"## Latest run detail — {latest_date}", "",
          "| Ticker | Opt volume | C/P today | Baseline status | z-score | OI | Price 1d% | Sector-wide | Flag |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(latest, key=lambda x: x["ticker"]):
        flag_mark = "⚑" if str(r.get("flagged")).lower() == "true" else ""
        L.append(f"| {r['ticker']} | {r.get('options_volume_total') or '—'} | {r.get('cp_ratio') or '—'} "
                  f"| {r.get('options_baseline_status') or '—'} | {r.get('zscore') or '—'} "
                  f"| {r.get('oi_classification') or '—'} | {r.get('price_chg_1d_pct') or '—'} "
                  f"| {r.get('sector_wide') or '—'} | {flag_mark} |")
    L.append("")

    tickers_seen = sorted({r["ticker"] for r in ticker_rows})
    L.append("## Per-ticker options-volume trend (most recent sessions)")
    L.append("")
    for t in tickers_seen:
        hist = [r for r in ticker_rows if r["ticker"] == t][-lookback:]
        if not hist:
            continue
        L.append(f"**{t}**")
        L.append("")
        L.append("| Date | Volume | C/P | z-score | OI | Price 1d% |")
        L.append("|---|---|---|---|---|---|")
        for r in hist:
            L.append(f"| {r['run_date']} | {r.get('options_volume_total') or '—'} | {r.get('cp_ratio') or '—'} "
                      f"| {r.get('zscore') or '—'} | {r.get('oi_classification') or '—'} "
                      f"| {r.get('price_chg_1d_pct') or '—'} |")
        L.append("")

    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data", help="folder containing dated run subfolders")
    ap.add_argument("--lookback", type=int, default=20, help="sessions to show in trend tables")
    args = ap.parse_args(argv)

    run_rows, ticker_rows = build_logs(args.data_dir)
    write_csv(args.data_dir / "run_log.csv", run_rows, RUN_LOG_COLS)
    write_csv(args.data_dir / "ticker_log.csv", ticker_rows, TICKER_LOG_COLS)

    md = render_dashboard(run_rows, ticker_rows, args.lookback)
    dash_path = args.data_dir / "dashboard.md"
    dash_path.write_text(md)

    print(f"Wrote {args.data_dir / 'run_log.csv'}")
    print(f"Wrote {args.data_dir / 'ticker_log.csv'}")
    print(f"Wrote {dash_path}")
    if run_rows:
        print(f"\n{len(run_rows)} run(s) logged, most recent: {run_rows[-1]['run_date']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
