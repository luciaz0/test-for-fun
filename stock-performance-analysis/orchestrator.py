#!/usr/bin/env python3
"""
Three-agent options-activity screener (Data -> Analysis -> Flagging), orchestrated with Claude.

    Data Agent      skills/yahoo-finance-market-data-collector/SKILL.md   sourced, dated Yahoo Finance data
                                                                          (watchlist + sector ETF / peers / SPY)
    Analysis Agent  skills/options-price-divergence-analyzer/SKILL.md     describes where options activity and price disagree
    Flagging Agent  skills/unusual-options-research-shortlister/SKILL.md  <=5-ticker research shortlist (never a trade signal)

Design principles
  * Numbers never pass through an LLM. Yahoo tools write values straight to the dated data file;
    ratios/z-scores are computed deterministically in Python and handed to the Analysis Agent.
  * Every figure carries source_url + fetched_at_utc (+ as_of). Missing -> "UNAVAILABLE", never estimated.
  * Open-interest change needs yesterday's OI. Yahoo only publishes *current* OI, so the pipeline keeps
    its own daily snapshots (data/history/options/<TICKER>/). Run it once per day, same time
    (pre-market ~08:30 ET recommended). OI-change and the options-volume baseline become available
    after 1 and ~20 daily runs respectively; until then they are reported as UNAVAILABLE /
    INSUFFICIENT_HISTORY.
  * Flags require an open-interest increase at the active strikes. --oi-missing block (default) drops
    flags whose OI change is unavailable; --oi-missing warn keeps them, capped at LOW confidence.
  * Sector-wide check: each ticker is compared with its SPDR sector ETF (from Yahoo's sector field, or
    --benchmarks overrides), optional peers, and SPY, all pulled from Yahoo with the same rules.
  * Output guardrails: JSON-schema tool outputs, a banned-language lint with re-prompt, a hard cap of
    5 flags, ticker/number cross-checks against the deterministic metrics, and confidence caps.

Usage
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=...            # optional: CLAUDE_MODEL=<model id>
    python orchestrator.py run --tickers AAPL MSFT NVDA AMD TSLA
    python orchestrator.py run --watchlist watchlist.txt --data-mode direct   # skip LLM in data step
    python orchestrator.py run --watchlist watchlist.txt --benchmarks benchmarks.json --oi-missing warn
    python orchestrator.py run --watchlist watchlist.txt --skip-data         # re-run steps 2-3 on today's file
    python orchestrator.py review                                           # fill 10-session outcomes in flag_log.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:  # imported lazily-tolerant so the metrics/validation code can be unit-tested offline
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None
try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None
try:  # load ANTHROPIC_API_KEY / CLAUDE_MODEL from a local .env file if present (never overrides a real env var)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger("screener")

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
SKILLS_DIR = ROOT / "skills"
UNAVAILABLE = "UNAVAILABLE"
YF_QUOTE = "https://finance.yahoo.com/quote/{t}"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")  # set CLAUDE_MODEL to your preferred model id

SKILL_DATA = "yahoo-finance-market-data-collector"
SKILL_ANALYSIS = "options-price-divergence-analyzer"
SKILL_FLAG = "unusual-options-research-shortlister"

# Yahoo `sector` field -> SPDR sector ETF (static, documented mapping; override per ticker with --benchmarks)
SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Industrials": "XLI",
    "Basic Materials": "XLB", "Utilities": "XLU", "Real Estate": "XLRE", "Communication Services": "XLC",
}


@dataclass
class Config:
    data_dir: Path = ROOT / "data"
    history_sessions: int = 90        # daily bars kept (trading sessions)
    avg_window: int = 30              # volume average window (sessions, excluding latest)
    events_window_days: int = 30      # calendar days ahead for scheduled events
    news_days: int = 7
    active_contracts: int = 15        # "most active strikes" = top N contracts by session volume
    max_expirations: int | None = None  # None = every listed expiration (complete totals)
    baseline_lookback: int = 60       # sessions of own options-volume history used for "normal range"
    baseline_min_days: int = 20       # below this -> INSUFFICIENT_HISTORY
    cp_window: int = 30
    cp_min_days: int = 10
    z_unusual: float = 2.0
    ratio_unusual: float = 2.0
    tilt_pct: float = 25.0            # C/P vs own average beyond +/-25% -> tilted
    flat_sigma: float = 1.0           # |1d move| < 1.0 sigma -> FLAT
    proxy_min_volume: int = 500       # day-one proxy: vol >= prior OI on a contract with >= 500 contracts
    max_flags: int = 5
    oi_missing_policy: str = "block"  # block | warn
    benchmarks_file: Path | None = None  # {"AAPL": {"sector_etf": "XLK", "peers": ["MSFT"]}}
    market_benchmark: str | None = "SPY"


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def num(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def rnd(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


def to_date(x: Any) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if isinstance(x, pd.Timestamp):
        return None if pd.isna(x) else x.date()
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return datetime.fromtimestamp(x, tz=timezone.utc).date()
    try:
        return pd.Timestamp(str(x)).date()
    except Exception:
        return None


def fld(value: Any, source_url: str, status: str = "OK", as_of: str | None = None,
        reason: str | None = None, **extra: Any) -> dict:
    """Every figure in the data file is wrapped with provenance."""
    d = {
        "value": UNAVAILABLE if status == "UNAVAILABLE" else value,
        "status": status,
        "source_url": source_url,
        "fetched_at_utc": iso(utcnow()),
        "as_of": as_of,
    }
    if reason:
        d["reason"] = reason
    d.update(extra)
    return d


def unavailable(source_url: str, reason: str) -> dict:
    return fld(None, source_url, "UNAVAILABLE", reason=reason)


def is_ok(f: dict | None) -> bool:
    return bool(f) and f.get("status") in ("OK", "PARTIAL")


def retry(fn: Callable[[], Any], attempts: int = 3, base: float = 1.5) -> Any:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - network layer
            last = e
            log.debug("retry %d/%d after error: %s", i + 1, attempts, e)
            time.sleep(base * (2 ** i))
    raise last  # type: ignore[misc]


def jdump(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(path)


def load_skill(name: str) -> str:
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    if text.startswith("---"):  # strip YAML frontmatter; the body is the system prompt
        text = text.split("---", 2)[2]
    return text.strip()


def ny_today() -> str:
    return datetime.now(NY).date().isoformat()


def third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def opex_context(session: str) -> dict:
    d = date.fromisoformat(session)
    nxt = third_friday(d.year, d.month)
    if nxt < d:
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        nxt = third_friday(y, m)
    prev = third_friday(d.year, d.month)
    if prev >= d:
        y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        prev = third_friday(y, m)
    return {
        "next_monthly_opex": nxt.isoformat(),
        "sessions_to_next_opex": int(np.busday_count(d, nxt)),
        "next_opex_is_quarterly": nxt.month in (3, 6, 9, 12),
        "previous_monthly_opex": prev.isoformat(),
        "sessions_since_previous_opex": int(np.busday_count(prev, d)),
    }


# --------------------------------------------------------------------------------------------------
# snapshot store (own history of Yahoo options pulls -> enables OI change and volume baselines)
# --------------------------------------------------------------------------------------------------
class SnapshotStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "history" / "options"

    def _dir(self, t: str) -> Path:
        return self.root / t.upper()

    def save(self, t: str, session: str, contracts: dict, aggregate: dict) -> None:
        jdump({"session_date": session, "contracts": contracts}, self._dir(t) / f"{session}.contracts.json")
        jdump({"session_date": session, **aggregate}, self._dir(t) / f"{session}.agg.json")

    def _sessions(self, t: str, kind: str, before: str) -> list[str]:
        d = self._dir(t)
        if not d.exists():
            return []
        stems = sorted(p.name.split(".")[0] for p in d.glob(f"*.{kind}.json"))
        return [s for s in stems if s < before]  # strictly prior sessions only

    def prior_contracts(self, t: str, session: str) -> dict | None:
        s = self._sessions(t, "contracts", session)
        return json.loads((self._dir(t) / f"{s[-1]}.contracts.json").read_text()) if s else None

    def prior_aggregates(self, t: str, session: str, lookback: int) -> list[dict]:
        s = self._sessions(t, "agg", session)[-lookback:]
        return [json.loads((self._dir(t) / f"{x}.agg.json").read_text()) for x in s]


# --------------------------------------------------------------------------------------------------
# Data Agent tools (Yahoo Finance only). Values go straight into self.records -> the dated file.
# --------------------------------------------------------------------------------------------------
REQUIRED_FIELDS = [
    "current_price", "current_volume", "market_state", "sector", "price_history_90d", "volume_vs_30d_avg",
    "options_volume_today", "active_contracts", "oi_change_active_strikes",
    "scheduled_events_30d", "news_7d",
]


class YahooDataTools:
    def __init__(self, cfg: Config, snaps: SnapshotStore):
        if yf is None:
            raise RuntimeError("yfinance is not installed: pip install yfinance")
        self.cfg, self.snaps = cfg, snaps
        self.records: dict[str, dict] = {}
        self._tk: dict[str, Any] = {}

    # -- plumbing -----------------------------------------------------------------------------------
    def _t(self, t: str):
        if t not in self._tk:
            self._tk[t] = yf.Ticker(t)
        return self._tk[t]

    def rec(self, t: str) -> dict:
        return self.records.setdefault(t, {"ticker": t, "session_date": None, "fields": {},
                                           "quality_notes": [], "agent_submitted": False})

    def dispatch(self, name: str, args: dict, expected_ticker: str | None = None) -> dict:
        t = str(args.get("ticker", "")).upper().strip()
        if expected_ticker and t != expected_ticker:
            return {"status": "ERROR", "error": f"This session is for {expected_ticker}; got {t!r}."}
        fn = getattr(self, name, None)
        if name.startswith("_") or fn is None or name not in {d["name"] for d in DATA_TOOLS}:
            return {"status": "ERROR", "error": f"unknown tool {name}"}
        try:
            return fn(**{**args, "ticker": t})
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed for %s", name, t)
            return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}

    # -- tools --------------------------------------------------------------------------------------
    def get_quote(self, ticker: str) -> dict:
        url, r = YF_QUOTE.format(t=ticker), self.rec(ticker)
        try:
            info = retry(lambda: self._t(ticker).info) or {}
            err = None
        except Exception as e:  # noqa: BLE001
            info, err = {}, str(e)
        price = num(info.get("regularMarketPrice") or info.get("currentPrice"))
        ts = info.get("regularMarketTime")
        as_of = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else None
        f = r["fields"]
        f["current_price"] = (fld(price, url, as_of=iso(as_of), currency=info.get("currency"))
                              if price is not None else
                              unavailable(url, err or "Yahoo quote returned no regularMarketPrice"))
        cv = num(info.get("regularMarketVolume"))
        f["current_volume"] = (fld(int(cv), url, as_of=iso(as_of)) if cv is not None
                               else unavailable(url, "regularMarketVolume not returned"))
        sec = info.get("sector")
        f["sector"] = (fld(sec, url, industry=info.get("industry")) if sec
                       else unavailable(url, "Yahoo returned no sector (ETFs and some tickers have none)"))
        ms = info.get("marketState")
        f["market_state"] = fld(ms, url, as_of=iso(as_of)) if ms else unavailable(url, "marketState not returned")
        if as_of:
            r["session_date"] = as_of.astimezone(NY).date().isoformat()
        age_h = rnd((utcnow() - as_of).total_seconds() / 3600, 1) if as_of else None
        return {"status": f["current_price"]["status"], "fields_written": ["current_price", "current_volume", "market_state", "sector"],
                "summary": {"market_state": ms, "sector": sec, "quote_time_utc": iso(as_of), "quote_age_hours": age_h,
                            "session_date": r["session_date"]},
                "reason": f["current_price"].get("reason")}

    def get_price_history(self, ticker: str) -> dict:
        url, r, cfg = YF_QUOTE.format(t=ticker) + "/history", self.rec(ticker), self.cfg
        f = r["fields"]
        try:
            hist = retry(lambda: self._t(ticker).history(period="1y", interval="1d",
                                                         auto_adjust=False, actions=False))
        except Exception as e:  # noqa: BLE001
            hist, err = None, str(e)
        else:
            err = "Yahoo returned no daily bars"
        if hist is None or hist.empty:
            f["price_history_90d"] = unavailable(url, err)
            f["volume_vs_30d_avg"] = unavailable(url, "no price history")
            return {"status": "UNAVAILABLE", "reason": err}
        hist = hist.dropna(subset=["Close"]).tail(cfg.history_sessions)
        bars = []
        for idx, row in hist.iterrows():
            v = num(row.get("Volume"))
            bars.append({"date": pd.Timestamp(idx).date().isoformat(), "open": num(row.get("Open")),
                         "high": num(row.get("High")), "low": num(row.get("Low")),
                         "close": num(row.get("Close")), "adj_close": num(row.get("Adj Close")),
                         "volume": int(v) if v is not None else None})
        status = "OK" if len(bars) >= cfg.history_sessions else "PARTIAL"
        f["price_history_90d"] = fld(bars, url, status=status, as_of=bars[-1]["date"],
                                     reason=None if status == "OK" else f"only {len(bars)} sessions available",
                                     sessions=len(bars))
        vols = [b["volume"] for b in bars]
        prev = [v for v in vols[-(cfg.avg_window + 1):-1] if v is not None]
        if vols[-1] is None or len(prev) < cfg.avg_window:
            f["volume_vs_30d_avg"] = unavailable(url, f"need latest volume + {cfg.avg_window} prior sessions")
        else:
            avg = sum(prev) / len(prev)
            f["volume_vs_30d_avg"] = fld({"latest_session_date": bars[-1]["date"], "latest_volume": vols[-1],
                                          "avg_prior_30_sessions": rnd(avg, 0),
                                          "ratio": rnd(vols[-1] / avg if avg else None, 3)},
                                         url, as_of=bars[-1]["date"])
        r["session_date"] = r["session_date"] or bars[-1]["date"]
        return {"status": status, "fields_written": ["price_history_90d", "volume_vs_30d_avg"],
                "summary": {"sessions": len(bars), "first": bars[0]["date"], "last": bars[-1]["date"],
                            "volume_ratio_status": f["volume_vs_30d_avg"]["status"]}}

    def get_options_activity(self, ticker: str) -> dict:
        url, r, cfg = YF_QUOTE.format(t=ticker) + "/options", self.rec(ticker), self.cfg
        f = r["fields"]
        session = r.get("session_date")
        if not session:
            return {"status": "ERROR", "error": "session date unknown: call get_quote or get_price_history first"}
        tk = self._t(ticker)
        try:
            exps = list(retry(lambda: tk.options))
        except Exception as e:  # noqa: BLE001
            exps, why = [], str(e)
        else:
            why = "Yahoo lists no option expirations for this ticker"
        if not exps:
            for k in ("options_volume_today", "active_contracts", "oi_change_active_strikes"):
                f[k] = unavailable(url, why)
            return {"status": "UNAVAILABLE", "reason": why}

        used = exps[: cfg.max_expirations] if cfg.max_expirations else exps
        contracts, failed, null_vol, stale = [], [], 0, 0
        for e in used:
            try:
                ch = retry(lambda e=e: tk.option_chain(e))
            except Exception:  # noqa: BLE001
                failed.append(e)
                continue
            for kind, df in (("call", ch.calls), ("put", ch.puts)):
                for row in df.to_dict("records"):
                    ltd = row.get("lastTradeDate")
                    ltd_d = None
                    if ltd is not None and not pd.isna(ltd):
                        ts = pd.Timestamp(ltd)
                        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
                        ltd_d = ts.tz_convert(NY).date().isoformat()
                    vol = num(row.get("volume"))
                    in_session = ltd_d == session
                    if vol is None and in_session:
                        null_vol += 1
                    if vol and not in_session:
                        stale += 1  # volume belongs to an earlier session: excluded, not carried forward
                    contracts.append({
                        "contract": row.get("contractSymbol"), "type": kind, "expiry": e,
                        "strike": num(row.get("strike")), "last_trade_date": ltd_d,
                        "volume_session": int(vol) if (vol and in_session) else 0,
                        "open_interest": int(num(row.get("openInterest"))) if num(row.get("openInterest")) is not None else None,
                        "implied_volatility": rnd(num(row.get("impliedVolatility"))),
                    })
        fetched = len(used) - len(failed)
        if fetched == 0:
            for k in ("options_volume_today", "active_contracts", "oi_change_active_strikes"):
                f[k] = unavailable(url, "every option_chain request failed")
            return {"status": "UNAVAILABLE", "reason": "every option_chain request failed"}
        status = "OK" if fetched == len(exps) else "PARTIAL"
        cov = {"expirations_fetched": fetched, "expirations_listed": len(exps), "failed_expirations": failed}
        calls = sum(c["volume_session"] for c in contracts if c["type"] == "call")
        puts = sum(c["volume_session"] for c in contracts if c["type"] == "put")
        agg = {"call_volume": calls, "put_volume": puts, "total_volume": calls + puts,
               "call_oi_total": sum(c["open_interest"] or 0 for c in contracts if c["type"] == "call"),
               "put_oi_total": sum(c["open_interest"] or 0 for c in contracts if c["type"] == "put"),
               "coverage_status": status, **cov}
        f["options_volume_today"] = fld(
            {"session_date": session, **agg,
             "contracts_null_volume_in_session": null_vol,
             "contracts_with_earlier_session_volume_excluded": stale},
            url, status=status, as_of=session,
            reason=None if status == "OK" else f"chain fetched for {fetched} of {len(exps)} expirations")

        active = sorted([c for c in contracts if c["volume_session"] > 0],
                        key=lambda c: c["volume_session"], reverse=True)[: cfg.active_contracts]
        f["active_contracts"] = fld(active, url, status=status, as_of=session,
                                    note="open_interest as published by Yahoo (OCC settlement of the prior session)")

        prior = self.snaps.prior_contracts(ticker, session)
        if prior is None:
            f["oi_change_active_strikes"] = unavailable(
                url, "No prior-session snapshot. Yahoo publishes current OI only; OI change needs consecutive daily runs.")
        else:
            pmap = prior["contracts"]
            rows = []
            for c in active:
                p = pmap.get(c["contract"])
                p_oi = p.get("open_interest") if p else None
                rows.append({"contract": c["contract"], "type": c["type"], "strike": c["strike"],
                             "expiry": c["expiry"], "volume_session": c["volume_session"],
                             "oi_prior_snapshot": p_oi, "oi_today": c["open_interest"],
                             "oi_change": (c["open_interest"] - p_oi) if (p_oi is not None and c["open_interest"] is not None) else UNAVAILABLE,
                             "volume_prior_snapshot": p.get("volume_session") if p else UNAVAILABLE})
            gap = int(np.busday_count(prior["session_date"], session))
            f["oi_change_active_strikes"] = fld(
                {"prior_snapshot_session": prior["session_date"], "sessions_between": gap, "contracts": rows},
                url, status=status, as_of=session,
                source_note="today's Yahoo OI minus OI in this pipeline's prior Yahoo snapshot")
        # persist today's snapshot (the prior snapshot is never overwritten or carried forward)
        self.snaps.save(ticker, session,
                        {c["contract"]: {"open_interest": c["open_interest"], "volume_session": c["volume_session"]}
                         for c in contracts if c["contract"]},
                        agg)
        return {"status": status, "fields_written": ["options_volume_today", "active_contracts",
                                                      "oi_change_active_strikes"],
                "summary": {"session_date": session, **cov, "contracts": len(contracts),
                            "contracts_null_volume_in_session": null_vol,
                            "contracts_with_earlier_session_volume_excluded": stale,
                            "oi_change_status": f["oi_change_active_strikes"]["status"],
                            "prior_snapshot": prior["session_date"] if prior else None}}

    def get_upcoming_events(self, ticker: str) -> dict:
        url, r = YF_QUOTE.format(t=ticker), self.rec(ticker)
        start = datetime.now(NY).date()
        end = start + timedelta(days=self.cfg.events_window_days)
        try:
            cal = retry(lambda: self._t(ticker).calendar)
        except Exception as e:  # noqa: BLE001
            r["fields"]["scheduled_events_30d"] = unavailable(url, str(e))
            return {"status": "UNAVAILABLE", "reason": str(e)}
        if isinstance(cal, pd.DataFrame):  # older yfinance shape
            cal = {k: list(v.dropna().values) for k, v in cal.T.to_dict("series").items()} if not cal.empty else {}
        cal = cal or {}
        out = {"window": [start.isoformat(), end.isoformat()]}
        for key, label in (("Earnings Date", "earnings"), ("Ex-Dividend Date", "ex_dividend"),
                           ("Dividend Date", "dividend_payment")):
            v = cal.get(key)
            if v is None or (isinstance(v, list) and not v):
                out[label] = {"status": UNAVAILABLE, "reason": f"Yahoo calendar has no '{key}'"}
                continue
            ds = [d for d in (to_date(x) for x in (v if isinstance(v, (list, tuple)) else [v])) if d]
            inwin = [d.isoformat() for d in ds if start <= d <= end]
            out[label] = {"status": "OK", "published_dates": [d.isoformat() for d in ds],
                          "in_window": inwin or "none_in_window",
                          **({"note": "two dates = Yahoo-published earnings window"} if label == "earnings" and len(ds) == 2 else {})}
        r["fields"]["scheduled_events_30d"] = fld(out, url, as_of=start.isoformat())
        return {"status": "OK", "fields_written": ["scheduled_events_30d"], "summary": out}

    def get_news(self, ticker: str) -> dict:
        url, r = YF_QUOTE.format(t=ticker) + "/news", self.rec(ticker)
        try:
            items = retry(lambda: self._t(ticker).news) or []
        except Exception as e:  # noqa: BLE001
            r["fields"]["news_7d"] = unavailable(url, str(e))
            return {"status": "UNAVAILABLE", "reason": str(e)}
        cutoff = utcnow() - timedelta(days=self.cfg.news_days)
        out = []
        for it in items:
            c = it.get("content", it)
            title = c.get("title")
            pub = c.get("pubDate") or c.get("displayTime") or it.get("providerPublishTime")
            ts = (datetime.fromtimestamp(pub, tz=timezone.utc) if isinstance(pub, (int, float))
                  else pd.Timestamp(pub).to_pydatetime() if pub else None)
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if not title or not ts or ts < cutoff:
                continue
            prov = c.get("provider", {})
            out.append({"title": title, "published_utc": iso(ts),
                        "publisher": prov.get("displayName") if isinstance(prov, dict) else it.get("publisher")})
        r["fields"]["news_7d"] = fld(out[:10], url, as_of=iso(utcnow()),
                                     source_note="Yahoo Finance news feed (may syndicate third-party publishers)")
        return {"status": "OK", "fields_written": ["news_7d"], "summary": {"headlines": len(out[:10])}}

    def submit_data_record(self, ticker: str, quality_notes: list[str] | None = None,
                           fields_unavailable: list[str] | None = None) -> dict:
        r = self.rec(ticker)
        r["quality_notes"] = [str(n) for n in (quality_notes or [])]
        actual = sorted(k for k, v in r["fields"].items() if v.get("status") == "UNAVAILABLE")
        claimed = sorted(set(fields_unavailable or []))
        if claimed != actual:
            r["quality_notes"].append(f"[orchestrator] agent-reported UNAVAILABLE {claimed} vs file {actual}; file is authoritative")
        r["agent_submitted"] = True
        return {"status": "OK", "missing_required_fields": [k for k in REQUIRED_FIELDS if k not in r["fields"]]}

    def finalize(self, t: str) -> dict:
        r = self.rec(t)
        for k in REQUIRED_FIELDS:
            if k not in r["fields"]:
                r["fields"][k] = unavailable(YF_QUOTE.format(t=t), "tool not called or did not return")
        if not r["agent_submitted"]:
            r["quality_notes"].append("[orchestrator] data agent did not submit a record; fields finalized automatically")
        return r


def _schema(props: dict, req: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": req}


_T = {"ticker": {"type": "string", "description": "Ticker symbol, e.g. AAPL"}}
DATA_TOOLS = [
    {"name": "get_quote", "description": "Yahoo Finance quote: current price, quote time, market state.",
     "input_schema": _schema(_T, ["ticker"])},
    {"name": "get_price_history", "description": "Yahoo Finance daily bars (last 90 sessions) and latest volume vs prior-30-session average.",
     "input_schema": _schema(_T, ["ticker"])},
    {"name": "get_options_activity", "description": "Yahoo Finance options chains: session call/put volume, most active contracts, OI change vs the pipeline's prior snapshot. Requires get_quote or get_price_history first.",
     "input_schema": _schema(_T, ["ticker"])},
    {"name": "get_upcoming_events", "description": "Yahoo Finance calendar: earnings, ex-dividend and dividend dates in the next 30 days.",
     "input_schema": _schema(_T, ["ticker"])},
    {"name": "get_news", "description": "Yahoo Finance news headlines from the last 7 days (title, publisher, time).",
     "input_schema": _schema(_T, ["ticker"])},
    {"name": "submit_data_record", "description": "Finish this ticker: data-quality notes only. Never include numbers you did not receive from a tool.",
     "input_schema": _schema({**_T, "quality_notes": {"type": "array", "items": {"type": "string"}},
                              "fields_unavailable": {"type": "array", "items": {"type": "string"}}},
                             ["ticker", "quality_notes", "fields_unavailable"])},
]


# --------------------------------------------------------------------------------------------------
# Claude plumbing + guardrails
# --------------------------------------------------------------------------------------------------
BASE_BANNED = [r"\bbullish\b", r"\bbearish\b", r"smart[- ]money", r"\bpositioning for\b", r"\bbetting on\b",
               r"\bprice target\b", r"\btarget price\b", r"\bentry\b", r"\bstop[- ]?loss"]
FLAG_BANNED = BASE_BANNED + [r"\bbuy\b", r"\bsell\b(?!-side)", r"\bposition siz", r"\ballocat",
                             r"\bexit (point|price|level)", r"\bgo(ing)? (long|short)\b",
                             r"\bconsider (buying|selling)\b", r"\btake a position\b"]


class LintError(RuntimeError):
    pass


def lint(obj: Any, patterns: list[str], path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(obj, str):
        for p in patterns:
            m = re.search(p, obj, flags=re.I)
            if m:
                hits.append(f"{path}: '{m.group(0)}'")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            hits += lint(v, patterns, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += lint(v, patterns, f"{path}[{i}]")
    return hits


def call_forced_tool(client, system: str, user_text: str, tool: dict, banned: list[str],
                     max_attempts: int = 3, max_tokens: int = 16000) -> dict:
    """Force a schema-checked tool call; reject and re-prompt on banned language."""
    messages: list[dict] = [{"role": "user", "content": user_text}]
    for attempt in range(1, max_attempts + 1):
        resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system, tools=[tool],
                                      tool_choice={"type": "tool", "name": tool["name"]}, messages=messages)
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(f"{tool['name']}: output truncated (max_tokens). Reduce --chunk-size.")
        block = next(b for b in resp.content if getattr(b, "type", None) == "tool_use")
        out = block.input
        hits = lint(out, banned)
        if not hits:
            return out
        log.warning("%s attempt %d rejected by lint: %s", tool["name"], attempt, hits[:5])
        messages += [{"role": "assistant", "content": resp.content},
                     {"role": "user", "content": [{
                         "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                         "content": "REJECTED by language lint (prohibited terms): " + "; ".join(hits[:20]) +
                                    ". Resubmit the COMPLETE output, describing activity without these terms."}]}]
    raise LintError(f"{tool['name']} failed language lint after {max_attempts} attempts")


# --------------------------------------------------------------------------------------------------
# Step 1 - Data Agent
# --------------------------------------------------------------------------------------------------
def _gather(client, tools: "YahooDataTools", system: str, t: str, run_date: str, mode: str, role: str) -> None:
    log.info("[data] %s (%s)", t, role)
    tools.rec(t)["role"] = role
    if mode == "direct":  # deterministic path, no LLM (cheaper; same tools, same file)
        for name in ("get_quote", "get_price_history", "get_options_activity", "get_upcoming_events", "get_news"):
            tools.dispatch(name, {"ticker": t})
        unav = [k for k, v in tools.rec(t)["fields"].items() if v["status"] == "UNAVAILABLE"]
        tools.submit_data_record(t, [f"direct mode; UNAVAILABLE: {unav}" if unav else "direct mode"], unav)
    else:
        what = "watchlist ticker" if role == "watchlist" else f"benchmark ticker ({role}; context for the sector-wide check)"
        messages: list[dict] = [{"role": "user", "content":
            f"Run date {run_date}. Gather data for {what} {t}: call get_quote, get_price_history, "
            f"get_options_activity, get_upcoming_events and get_news, then submit_data_record."}]
        for _ in range(14):
            resp = client.messages.create(model=MODEL, max_tokens=2000, system=system,
                                          tools=DATA_TOOLS, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for b in resp.content:
                if getattr(b, "type", None) == "tool_use":
                    out = tools.dispatch(b.name, b.input, expected_ticker=t)
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)})
            messages.append({"role": "user", "content": results})
            if tools.rec(t)["agent_submitted"]:
                break
    tools.finalize(t)


def resolve_benchmarks(cfg: Config, records: dict, tickers: list[str]) -> dict:
    """ticker -> sector ETF / peers / market. Sector comes from Yahoo; never guessed."""
    user: dict = {}
    if cfg.benchmarks_file:
        user = {k.upper(): v for k, v in json.loads(Path(cfg.benchmarks_file).read_text()).items()}
    out = {}
    for t in tickers:
        u = user.get(t, {})
        sf = records[t]["fields"].get("sector", {})
        sector = sf.get("value") if is_ok(sf) else None
        etf = (u.get("sector_etf") or SECTOR_ETF.get(sector or "") or "").upper() or None
        src = ("user benchmarks file" if u.get("sector_etf") else
               "Yahoo sector field -> SPDR sector ETF map" if etf else
               "UNAVAILABLE: Yahoo returned no sector" if not sector else f"UNAVAILABLE: no ETF mapped for sector {sector!r}")
        out[t] = {"sector": sector or UNAVAILABLE, "sector_etf": etf or UNAVAILABLE, "sector_etf_source": src,
                  "peers": [p.upper() for p in u.get("peers", []) if p.upper() != t],
                  "peers_source": "user benchmarks file" if u.get("peers") else "none supplied",
                  "market": cfg.market_benchmark.upper() if cfg.market_benchmark else UNAVAILABLE}
    return out


def run_data_agent(client, cfg: Config, tickers: list[str], run_date: str, mode: str) -> Path:
    tools = YahooDataTools(cfg, SnapshotStore(cfg.data_dir))
    system = load_skill(SKILL_DATA)
    for t in tickers:
        _gather(client, tools, system, t, run_date, mode, "watchlist")

    bmap = resolve_benchmarks(cfg, tools.records, tickers)
    roles: dict[str, str] = {}
    for b in bmap.values():
        for sym, role in [(b["sector_etf"], "sector_etf"), (b["market"], "market")] + [(p, "peer") for p in b["peers"]]:
            if sym and sym != UNAVAILABLE and sym not in tickers:
                roles.setdefault(sym, role)
    for sym, role in roles.items():
        _gather(client, tools, system, sym, run_date, mode, role)

    payload = {"schema_version": 2, "run_date": run_date, "generated_at_utc": iso(utcnow()),
               "source": "Yahoo Finance (finance.yahoo.com) via yfinance " + getattr(yf, "__version__", "?"),
               "data_mode": mode, "tickers": tickers, "benchmark_tickers": list(roles),
               "benchmark_map": bmap, "records": tools.records}
    path = cfg.data_dir / run_date / f"data_agent_{run_date}.json"
    jdump(payload, path)
    log.info("[data] wrote %s", path)
    return path


# --------------------------------------------------------------------------------------------------
# Deterministic metrics (Python does the arithmetic; the Analysis Agent describes it)
# --------------------------------------------------------------------------------------------------
def compute_metrics(rec: dict, snaps: SnapshotStore, cfg: Config) -> dict:
    t, f, session = rec["ticker"], rec["fields"], rec.get("session_date")
    m: dict[str, Any] = {"session_date": session}

    # options volume vs own history ---------------------------------------------------------------
    ov = f.get("options_volume_today", {})
    z = ratio = cp_today = None
    if is_ok(ov) and session:
        v = ov["value"]
        cp_today = v["call_volume"] / v["put_volume"] if v["put_volume"] else None
        m["options_today"] = {"total": v["total_volume"], "calls": v["call_volume"], "puts": v["put_volume"],
                              "cp_ratio": rnd(cp_today, 3), "coverage": ov["status"]}
        hist = [h for h in snaps.prior_aggregates(t, session, cfg.baseline_lookback)
                if h.get("coverage_status") == ov["status"]]  # compare like-for-like chain coverage
        tots = np.array([h["total_volume"] for h in hist], float)
        if len(tots) >= cfg.baseline_min_days:
            mean, med, std = tots.mean(), float(np.median(tots)), tots.std(ddof=1)
            z = (v["total_volume"] - mean) / std if std > 0 else None
            ratio = v["total_volume"] / med if med > 0 else None
            m["options_baseline"] = {"status": "OK", "n_days": len(tots), "mean": rnd(mean, 0), "median": rnd(med, 0),
                                     "p10": rnd(np.percentile(tots, 10), 0), "p90": rnd(np.percentile(tots, 90), 0),
                                     "min": rnd(tots.min(), 0), "max": rnd(tots.max(), 0),
                                     "zscore": rnd(z, 2), "ratio_to_median": rnd(ratio, 2),
                                     "percentile_rank": rnd(float((tots < v["total_volume"]).mean() * 100), 1)}
        else:
            m["options_baseline"] = {"status": "INSUFFICIENT_HISTORY", "n_days": int(len(tots)),
                                     "needed": cfg.baseline_min_days}
        cph = hist[-cfg.cp_window:]
        c_sum, p_sum = sum(h["call_volume"] for h in cph), sum(h["put_volume"] for h in cph)
        if len(cph) >= cfg.cp_min_days and p_sum > 0 and cp_today is not None:
            avg = c_sum / p_sum
            pct = (cp_today / avg - 1) * 100
            tilt = "CALL-TILTED" if pct > cfg.tilt_pct else "PUT-TILTED" if pct < -cfg.tilt_pct else "NO-TILT"
            m["cp_ratio"] = {"today": rnd(cp_today, 3), "avg_30d": rnd(avg, 3), "pct_diff": rnd(pct, 1),
                             "n_days": len(cph), "method": "sum(calls)/sum(puts) over window", "tilt": tilt}
        else:
            m["cp_ratio"] = {"today": rnd(cp_today, 3), "avg_30d": None, "n_days": len(cph),
                             "tilt": UNAVAILABLE, "reason": "insufficient C/P history or zero puts"}
    else:
        m["options_today"] = {"status": UNAVAILABLE}
        m["options_baseline"] = {"status": UNAVAILABLE}
        m["cp_ratio"] = {"tilt": UNAVAILABLE}

    # day-one proxy: session volume >= published (prior-settlement) OI ------------------------------
    ac = f.get("active_contracts", {})
    proxy = []
    if is_ok(ac):
        for c in ac["value"]:
            oi = c.get("open_interest")
            if oi and c["volume_session"] >= cfg.proxy_min_volume and c["volume_session"] >= oi:
                proxy.append({"contract": c["contract"], "volume": c["volume_session"], "prior_oi": oi,
                              "vol_to_prior_oi": rnd(c["volume_session"] / oi, 2)})
    m["vol_oi_proxy"] = {"hits": proxy, "max_vol_to_prior_oi": max((p["vol_to_prior_oi"] for p in proxy), default=None)}

    # open-interest change -------------------------------------------------------------------------
    oc = f.get("oi_change_active_strikes", {})
    if is_ok(oc):
        rows = [r for r in oc["value"]["contracts"] if isinstance(r.get("oi_change"), (int, float))]
        up = sum(r["oi_change"] > 0 for r in rows)
        dn = sum(r["oi_change"] < 0 for r in rows)
        flat = len(rows) - up - dn
        if not rows:
            cls = UNAVAILABLE
        elif flat == len(rows):
            cls = "UNAVAILABLE_STALE"  # every active contract unchanged: Yahoo OI likely not yet updated
        elif up / len(rows) >= 0.6:
            cls = "OPENED"
        elif dn / len(rows) >= 0.6:
            cls = "CLOSED"
        else:
            cls = "MIXED"
        m["oi"] = {"classification": cls, "n_contracts": len(rows), "n_up": up, "n_down": dn, "n_unchanged": flat,
                   "net_oi_change": sum(r["oi_change"] for r in rows),
                   "net_call_oi_change": sum(r["oi_change"] for r in rows if r["type"] == "call"),
                   "net_put_oi_change": sum(r["oi_change"] for r in rows if r["type"] == "put"),
                   "prior_snapshot_session": oc["value"]["prior_snapshot_session"],
                   "sessions_between": oc["value"]["sessions_between"],
                   "timing_note": "Yahoo OI reflects prior-session OCC settlement; the change mostly reflects "
                                  "activity in the session(s) since the prior snapshot."}
    else:
        m["oi"] = {"classification": UNAVAILABLE, "reason": oc.get("reason")}

    # price action ---------------------------------------------------------------------------------
    ph = f.get("price_history_90d", {})
    move = UNAVAILABLE
    if is_ok(ph) and len(ph["value"]) >= 22:
        c = np.array([b["close"] for b in ph["value"]], float)
        rets = np.diff(c) / c[:-1]
        sig = float(np.std(rets[-21:-1], ddof=1))
        r1 = float(rets[-1])
        ms = r1 / sig if sig > 0 else None
        move = UNAVAILABLE if ms is None else ("FLAT" if abs(ms) < cfg.flat_sigma else "UP" if r1 > 0 else "DOWN")
        vr = f.get("volume_vs_30d_avg", {})
        m["price"] = {"last_close": rnd(c[-1], 4), "bar_date": ph["value"][-1]["date"],
                      "chg_1d_pct": rnd(r1 * 100, 2),
                      "chg_5d_pct": rnd((c[-1] / c[-6] - 1) * 100, 2),
                      "chg_20d_pct": rnd((c[-1] / c[-21] - 1) * 100, 2),
                      "sigma_20d_daily_pct": rnd(sig * 100, 2), "move_1d_sigma": rnd(ms, 2),
                      "pos_in_90d_range_pct": rnd((c[-1] - c.min()) / (c.max() - c.min()) * 100 if c.max() > c.min() else None, 1),
                      "stock_volume_ratio_30d": vr["value"]["ratio"] if is_ok(vr) else UNAVAILABLE,
                      "move_label": move}
    else:
        m["price"] = {"move_label": UNAVAILABLE}

    tilt = m["cp_ratio"].get("tilt", UNAVAILABLE)
    if UNAVAILABLE in (tilt, move):
        rel = "UNDETERMINABLE"
    elif move == "FLAT":
        rel = "NOT_MOVED"
    elif tilt == "NO-TILT":
        rel = "UNDETERMINABLE"
    else:
        rel = "CONFIRMS" if (tilt == "CALL-TILTED") == (move == "UP") else "CONTRADICTS"
    m["price_vs_options"] = {"price_move": move, "options_tilt": tilt, "relationship": rel}

    # events ---------------------------------------------------------------------------------------
    ev = f.get("scheduled_events_30d", {})
    evs = {}
    if is_ok(ev):
        for k in ("earnings", "ex_dividend", "dividend_payment"):
            e = ev["value"].get(k, {})
            evs[k] = e.get("in_window") if e.get("status") == "OK" else UNAVAILABLE
    m["events"] = {**({"calendar": evs} if evs else {"calendar": UNAVAILABLE}),
                   **(opex_context(session) if session else {})}

    # deterministic screen + ranking key -----------------------------------------------------------
    b = m["options_baseline"]
    vol_unusual = b.get("status") == "OK" and ((z or 0) >= cfg.z_unusual or (ratio or 0) >= cfg.ratio_unusual)
    proxy_fires = b.get("status") == "INSUFFICIENT_HISTORY" and bool(proxy)
    oi_cls = m["oi"]["classification"]
    m["deterministic_screens"] = {
        "volume_unusual_vs_own_history": vol_unusual, "proxy_only_unusual": proxy_fires,
        "oi_opened": oi_cls == "OPENED", "oi_unavailable": oi_cls.startswith(UNAVAILABLE),
        "oi_criterion_met": oi_allowed(oi_cls, cfg),
        "price_not_moved_or_contradicts": rel in ("NOT_MOVED", "CONTRADICTS"),
        "flag_candidate": (vol_unusual or proxy_fires) and oi_allowed(oi_cls, cfg)
                          and rel in ("NOT_MOVED", "CONTRADICTS"),
    }
    m["rank_key"] = [0 if vol_unusual else 1 if proxy_fires else 2, -(z or 0), -(ratio or 0),
                     -(m["vol_oi_proxy"]["max_vol_to_prior_oi"] or 0)]
    return m


def oi_allowed(oi_cls: str, cfg: Config) -> bool:
    """Flags need an OI increase. Missing OI is allowed only under --oi-missing warn."""
    return oi_cls == "OPENED" or (cfg.oi_missing_policy == "warn" and oi_cls.startswith(UNAVAILABLE))


def sector_context(t: str, bm: dict | None, mall: dict[str, dict]) -> dict:
    """Is the same unusual options volume showing up in the sector ETF / peers (vs THEIR own history)?"""
    if not bm:
        return {"sector_wide": UNAVAILABLE, "reason": "no benchmark map in data file", "benchmarks": []}
    rows = []
    pairs = [(bm.get("sector_etf"), "sector_etf"), (bm.get("market"), "market")] + [(p, "peer") for p in bm.get("peers", [])]
    for sym, role in pairs:
        if not sym or sym == UNAVAILABLE or sym == t:
            continue
        mm = mall.get(sym)
        if mm is None:
            rows.append({"ticker": sym, "role": role, "status": UNAVAILABLE, "reason": "benchmark not in data file"})
            continue
        b = mm["options_baseline"]
        rows.append({"ticker": sym, "role": role, "baseline_status": b.get("status"),
                     "zscore": b.get("zscore"), "ratio_to_median": b.get("ratio_to_median"),
                     "volume_unusual_vs_own_history": mm["deterministic_screens"]["volume_unusual_vs_own_history"],
                     "cp_tilt": mm["cp_ratio"].get("tilt"), "chg_1d_pct": mm["price"].get("chg_1d_pct"),
                     "move_1d_sigma": mm["price"].get("move_1d_sigma"), "oi_classification": mm["oi"]["classification"]})
    peers = [r for r in rows if r["role"] in ("sector_etf", "peer") and r.get("baseline_status") == "OK"]
    sw = "YES" if any(r["volume_unusual_vs_own_history"] for r in peers) else "NO" if peers else UNAVAILABLE
    etf = next((r for r in rows if r["role"] == "sector_etf" and r.get("chg_1d_pct") is not None), None)
    own = mall[t]["price"].get("chg_1d_pct")
    mkt = [r for r in rows if r["role"] == "market" and r.get("baseline_status") == "OK"]
    return {"sector": bm.get("sector"), "sector_etf": bm.get("sector_etf"), "peers": bm.get("peers", []),
            "benchmarks": rows, "sector_wide": sw,
            "sector_wide_rule": "YES if the sector ETF or any peer has options volume unusual vs its own history "
                                "(same z/ratio thresholds); UNAVAILABLE if none has a usable baseline",
            "move_vs_sector_etf_1d_pct": rnd(own - etf["chg_1d_pct"], 2) if (etf and own is not None) else UNAVAILABLE,
            "market_options_volume_unusual": (any(r["volume_unusual_vs_own_history"] for r in mkt) if mkt else UNAVAILABLE)}


def trimmed_record(rec: dict) -> dict:
    """What the Analysis Agent sees from the data file (full file stays on disk)."""
    f = json.loads(json.dumps(rec["fields"], default=str))
    if is_ok(f.get("price_history_90d")):
        f["price_history_90d"]["value"] = f["price_history_90d"]["value"][-5:]
        f["price_history_90d"]["note"] = "truncated to last 5 bars for the prompt; full series in data file"
    return {"ticker": rec["ticker"], "session_date": rec.get("session_date"),
            "quality_notes": rec.get("quality_notes", []), "fields": f}


# --------------------------------------------------------------------------------------------------
# Step 2 - Analysis Agent
# --------------------------------------------------------------------------------------------------
_S, _N = {"type": "string"}, {"type": ["number", "null"]}
ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "Submit the descriptive analysis for every ticker in the input.",
    "input_schema": _schema({
        "tickers": {"type": "array", "items": _schema({
            "ticker": _S, "session_date": {"type": ["string", "null"]},
            "options_volume_vs_normal": _schema({
                "status": {"enum": ["OK", "INSUFFICIENT_HISTORY", "UNAVAILABLE"]},
                "zscore": _N, "ratio_to_median": _N, "history_days": {"type": ["integer", "null"]},
                "description": _S}, ["status", "description"]),
            "call_put_ratio": _schema({"today": _N, "avg_30d": _N, "pct_diff": _N, "description": _S},
                                      ["description"]),
            "open_interest": _schema({"classification": {"enum": ["OPENED", "CLOSED", "MIXED", "UNAVAILABLE",
                                                                  "UNAVAILABLE_STALE"]},
                                      "session_covered": _S, "description": _S}, ["classification", "description"]),
            "price_vs_options": _schema({"price_move": {"enum": ["UP", "DOWN", "FLAT", "UNAVAILABLE"]},
                                         "options_tilt": {"enum": ["CALL-TILTED", "PUT-TILTED", "NO-TILT", "UNAVAILABLE"]},
                                         "relationship": {"enum": ["CONFIRMS", "CONTRADICTS", "NOT_MOVED", "UNDETERMINABLE"]},
                                         "description": _S}, ["price_move", "options_tilt", "relationship", "description"]),
            "scheduled_events": _schema({"could_explain": {"type": "boolean"}, "description": _S},
                                        ["could_explain", "description"]),
            "sector_context": _schema({"sector_wide": {"enum": ["YES", "NO", "UNAVAILABLE"]}, "description": _S},
                                      ["sector_wide", "description"]),
            "flagged": {"type": "boolean"}, "flag_basis": _S,
            "criteria_missed": {"type": "array", "items": _S},
            "cannot_determine": {"type": "array", "items": _S, "minItems": 4},
            "data_caveats": {"type": "array", "items": _S}},
            ["ticker", "options_volume_vs_normal", "call_put_ratio", "open_interest", "price_vs_options",
             "scheduled_events", "sector_context", "flagged", "cannot_determine"])},
        "ranked_flags": {"type": "array", "items": _S},
        "run_caveats": {"type": "array", "items": _S}}, ["tickers", "ranked_flags"]),
}

STANDARD_CANNOT_DETERMINE = [
    "Whether the volume was buying or selling: every contract has a buyer and a seller and Yahoo data carries no trade side.",
    "Whether each trade was opening or closing for the counterparty.",
    "Whether the activity is directional or a hedge (e.g., protective puts, calls against a short, dealer hedging).",
    "Who traded: participant identity is not observable in this data.",
]


def validate_analysis(out: dict, metrics: dict[str, dict], tickers: list[str], cfg: Config) -> dict:
    by_t = {e["ticker"].upper(): e for e in out.get("tickers", []) if e.get("ticker")}
    entries = []
    for t in tickers:
        m = metrics[t]
        e = by_t.get(t)
        if e is None:
            e = {"ticker": t, "flagged": False, "cannot_determine": [], "data_caveats": [],
                 "options_volume_vs_normal": {"status": UNAVAILABLE, "description": "Analysis agent omitted this ticker."},
                 "call_put_ratio": {"description": "omitted"}, "open_interest": {"classification": UNAVAILABLE, "description": "omitted"},
                 "price_vs_options": {**m["price_vs_options"], "description": "omitted"},
                 "scheduled_events": {"could_explain": False, "description": "omitted"},
                 "sector_context": {"sector_wide": m["sector_context"]["sector_wide"], "description": "omitted"}}
        caveats = e.setdefault("data_caveats", [])
        # cross-check every number the agent reported against the deterministic metrics
        b, ov = m["options_baseline"], e["options_volume_vs_normal"]
        for key, ref in (("zscore", b.get("zscore")), ("ratio_to_median", b.get("ratio_to_median"))):
            if ov.get(key) is not None and (ref is None or abs(ov[key] - ref) > 0.05):
                caveats.append(f"[orchestrator] {key} {ov[key]} replaced by computed {ref}")
            ov[key] = ref
        ov["history_days"] = b.get("n_days")
        ov["status"] = b.get("status", UNAVAILABLE) if b.get("status") in ("OK", "INSUFFICIENT_HISTORY") else UNAVAILABLE
        cp = m["cp_ratio"]
        e["call_put_ratio"].update({"today": cp.get("today"), "avg_30d": cp.get("avg_30d"), "pct_diff": cp.get("pct_diff")})
        if e["open_interest"].get("classification") != m["oi"]["classification"]:
            caveats.append(f"[orchestrator] OI classification {e['open_interest'].get('classification')} "
                           f"replaced by computed {m['oi']['classification']}")
            e["open_interest"]["classification"] = m["oi"]["classification"]
        for k in ("price_move", "options_tilt", "relationship"):
            e["price_vs_options"][k] = m["price_vs_options"][k]
        sc = e.setdefault("sector_context", {"description": ""})
        sc["sector_wide"] = m["sector_context"]["sector_wide"]
        oi_cls = m["oi"]["classification"]
        if e.get("flagged") and not oi_allowed(oi_cls, cfg):
            e["flagged"] = False
            caveats.append(f"[orchestrator] unflagged: OI {oi_cls} does not confirm new positions "
                           f"(oi_missing_policy={cfg.oi_missing_policy})")
        cd = e.get("cannot_determine") or []
        if not any("buy" in s.lower() for s in cd):
            cd = STANDARD_CANNOT_DETERMINE + cd
        e["cannot_determine"] = cd
        e["session_date"] = m.get("session_date")
        entries.append(e)
    # rank strictly by unusualness vs own history (deterministic), keeping the agent's flag decisions
    flagged = [e["ticker"] for e in entries if e.get("flagged")]
    ranked = sorted(flagged, key=lambda t: metrics[t]["rank_key"])
    return {"tickers": entries, "ranked_flags": ranked, "run_caveats": out.get("run_caveats", [])}


def run_analysis_agent(client, cfg: Config, data_path: Path, chunk_size: int) -> Path:
    data = json.loads(data_path.read_text())
    run_date, tickers = data["run_date"], data["tickers"]
    snaps = SnapshotStore(cfg.data_dir)
    bench = data.get("benchmark_tickers", [])
    mall = {t: compute_metrics(data["records"][t], snaps, cfg) for t in tickers + bench if t in data["records"]}
    for t in tickers:
        mall[t]["sector_context"] = sector_context(t, data.get("benchmark_map", {}).get(t), mall)
    metrics = {t: mall[t] for t in tickers}
    bench_metrics = {t: mall[t] for t in bench if t in mall}
    system = load_skill(SKILL_ANALYSIS)
    merged: dict[str, Any] = {"tickers": [], "run_caveats": []}
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        log.info("[analysis] %s", chunk)
        payload = {"run_date": run_date, "data_file": str(data_path), "source": data["source"],
                   "thresholds": {"z_unusual": cfg.z_unusual, "ratio_unusual": cfg.ratio_unusual,
                                  "tilt_pct": cfg.tilt_pct, "flat_sigma": cfg.flat_sigma,
                                  "proxy_min_volume": cfg.proxy_min_volume, "baseline_min_days": cfg.baseline_min_days},
                   "oi_missing_policy": cfg.oi_missing_policy,
                   "records": [{**trimmed_record(data["records"][t]), "metrics": metrics[t]} for t in chunk]}
        out = call_forced_tool(client, system,
                               "Analyze these records per your skill and submit via submit_analysis.\n\n"
                               + json.dumps(payload, default=str), ANALYSIS_TOOL, BASE_BANNED)
        merged["tickers"] += out.get("tickers", [])
        merged["run_caveats"] += out.get("run_caveats", [])
    result = validate_analysis(merged, metrics, tickers, cfg)
    result.update({"run_date": run_date, "data_file": str(data_path), "metrics": metrics,
                   "benchmark_metrics": bench_metrics, "oi_missing_policy": cfg.oi_missing_policy,
                   "generated_at_utc": iso(utcnow())})
    path = cfg.data_dir / run_date / f"analysis_{run_date}.json"
    jdump(result, path)
    (path.with_suffix(".md")).write_text(render_analysis_md(result))
    log.info("[analysis] wrote %s (%d flagged)", path, len(result["ranked_flags"]))
    return path


def render_analysis_md(a: dict) -> str:
    L = [f"# Options-activity analysis — {a['run_date']}", "",
         "_Descriptive only. Not a recommendation. Source: Yahoo Finance (delayed)._", "",
         f"_OI policy: {a.get('oi_missing_policy', 'block')} — flags require an open-interest increase"
         f"{'' if a.get('oi_missing_policy', 'block') == 'block' else ' (or unavailable OI, capped LOW)'}._", "",
         "| Ticker | Opt vol vs own history | C/P today vs 30d | OI at active strikes | Price vs options | Event could explain | Sector-wide | Flag |",
         "|---|---|---|---|---|---|---|---|"]
    for e in a["tickers"]:
        ov, cp = e["options_volume_vs_normal"], e["call_put_ratio"]
        vol = (f"z {ov.get('zscore')} / {ov.get('ratio_to_median')}× median (n={ov.get('history_days')})"
               if ov["status"] == "OK" else f"{ov['status']} (n={ov.get('history_days')})")
        cps = f"{cp.get('today')} vs {cp.get('avg_30d')} ({cp.get('pct_diff')}%)" if cp.get("avg_30d") else f"{cp.get('today')} vs n/a"
        pv = e["price_vs_options"]
        L.append(f"| {e['ticker']} | {vol} | {cps} | {e['open_interest']['classification']} | "
                 f"{pv['price_move']} / {pv['options_tilt']} → {pv['relationship']} | "
                 f"{'yes' if e['scheduled_events'].get('could_explain') else 'no'} | "
                 f"{e.get('sector_context', {}).get('sector_wide', UNAVAILABLE)} | {'⚑' if e.get('flagged') else ''} |")
    L += ["", f"**Ranked flags:** {', '.join(a['ranked_flags']) or 'none'}", ""]
    for e in a["tickers"]:
        if not e.get("flagged"):
            continue
        L += [f"## {e['ticker']}", "", e.get("flag_basis", ""), "",
              f"- Options volume: {e['options_volume_vs_normal']['description']}",
              f"- Call/put: {e['call_put_ratio']['description']}",
              f"- Open interest: {e['open_interest']['description']}",
              f"- Price vs options: {e['price_vs_options']['description']}",
              f"- Scheduled events: {e['scheduled_events']['description']}",
              f"- Sector context: {e.get('sector_context', {}).get('description', '')}", "", "**Cannot determine:**"]
        L += [f"- {s}" for s in e["cannot_determine"]]
        if e.get("data_caveats"):
            L += ["", "**Data caveats:**"] + [f"- {s}" for s in e["data_caveats"]]
        L.append("")
    if a.get("run_caveats"):
        L += ["## Run caveats"] + [f"- {s}" for s in a["run_caveats"]]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------------------------------
# Step 3 - Flagging Agent
# --------------------------------------------------------------------------------------------------
SHORTLIST_TOOL = {
    "name": "submit_shortlist",
    "description": "Submit the research shortlist (0-5 tickers).",
    "input_schema": _schema({
        "nothing_unusual": {"type": "boolean"},
        "items": {"type": "array", "maxItems": 5, "items": _schema({
            "ticker": _S, "whats_unusual": _S,
            "oi_confirmation": {"enum": ["YES — opened", "NO — closed", "MIXED", "UNAVAILABLE"]},
            "oi_detail": _S, "sector_comparison": _S,
            "boring_explanation": _S, "boring_explanation_verified": {"type": "boolean"},
            "research_next": {"type": "array", "items": _S, "minItems": 1},
            "confidence": {"enum": ["LOW", "MEDIUM", "HIGH"]}, "confidence_reason": _S},
            ["ticker", "whats_unusual", "oi_confirmation", "sector_comparison", "boring_explanation", "research_next",
             "confidence", "confidence_reason"])},
        "research_first_question": _S}, ["nothing_unusual", "items"]),
}
_CONF = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def cap_confidence(item: dict, m: dict) -> None:
    """Hard caps so a model can't talk itself into confidence the data doesn't support."""
    b, oi = m["options_baseline"], m["oi"]["classification"]
    cov = m.get("options_today", {}).get("coverage")
    ev = m["events"]
    cap, why = "HIGH", []
    if b.get("status") != "OK":
        cap, why = "LOW", why + ["options-volume history insufficient"]
    if oi != "OPENED":
        cap, why = "LOW", why + [f"OI {oi}"]
    if cov == "PARTIAL":
        cap, why = "LOW", why + ["partial options-chain coverage"]
    sw = m.get("sector_context", {}).get("sector_wide", UNAVAILABLE)
    if sw == "YES":
        cap, why = "LOW", why + ["sector ETF/peers show the same unusual volume"]
    elif sw != "NO" and cap == "HIGH":
        cap, why = "MEDIUM", why + ["sector check UNAVAILABLE"]
    if cap == "HIGH":
        z = b.get("zscore") or 0
        cal = ev.get("calendar", {})
        has_event = isinstance(cal, dict) and any(isinstance(v, list) for v in cal.values())
        if z < 3 or m["price_vs_options"]["relationship"] != "NOT_MOVED" or has_event or ev.get("sessions_to_next_opex", 99) <= 5:
            cap, why = "MEDIUM", why + ["HIGH criteria not all met"]
    if _CONF[item["confidence"]] > _CONF[cap]:
        item["confidence_reason"] += f" [capped {item['confidence']}→{cap} by orchestrator: {', '.join(why)}]"
        item["confidence"] = cap


def validate_shortlist(out: dict, analysis: dict, cfg: Config) -> dict:
    known = {e["ticker"] for e in analysis["tickers"]}
    seen, items = set(), []
    for it in out.get("items", []):
        t = it.get("ticker", "").upper()
        if t not in known or t in seen:
            log.warning("[flag] dropped item for unknown/duplicate ticker %r", t)
            continue
        m = analysis["metrics"][t]
        if not oi_allowed(m["oi"]["classification"], cfg):
            log.warning("[flag] dropped %s: OI %s does not confirm new positions (policy=%s)",
                        t, m["oi"]["classification"], cfg.oi_missing_policy)
            continue
        seen.add(t)
        it["ticker"] = t
        cap_confidence(it, m)
        items.append(it)
    if len(items) > cfg.max_flags:
        log.warning("[flag] %d items returned; truncating to %d", len(items), cfg.max_flags)
        items = items[: cfg.max_flags]
    return {"nothing_unusual": not items, "items": items,
            "research_first_question": out.get("research_first_question", "") if items else ""}


def run_flagging_agent(client, cfg: Config, analysis_path: Path) -> Path:
    a = json.loads(analysis_path.read_text())
    run_date = a["run_date"]
    system = load_skill(SKILL_FLAG)
    view = {k: a[k] for k in ("run_date", "ranked_flags", "run_caveats", "tickers")}
    view["metrics"] = {t: a["metrics"][t] for t in a["metrics"]}
    view["oi_missing_policy"] = cfg.oi_missing_policy
    out = call_forced_tool(client, system,
                           f"Analysis output for {len(a['tickers'])} tickers. Build the research shortlist "
                           f"and submit via submit_shortlist.\n\n" + json.dumps(view, default=str),
                           SHORTLIST_TOOL, FLAG_BANNED)
    sl = validate_shortlist(out, a, cfg)
    sl.update({"run_date": run_date, "analysis_file": str(analysis_path), "generated_at_utc": iso(utcnow())})
    jpath = cfg.data_dir / run_date / f"shortlist_{run_date}.json"
    jdump(sl, jpath)
    md = render_shortlist_md(sl, len(a["tickers"]))
    mpath = jpath.with_suffix(".md")
    mpath.write_text(md)
    append_flag_log(cfg, sl, a)
    log.info("[flag] wrote %s", mpath)
    return mpath


def render_shortlist_md(sl: dict, n: int) -> str:
    if sl["nothing_unusual"]:
        return f"No genuinely unusual options activity across {n} tickers for session {sl['run_date']}.\n"
    L = [f"# Research shortlist — {sl['run_date']}", "",
         "_A list of things to look at, not a trading signal. Source: Yahoo Finance (delayed). "
         "Verify every figure at the source before relying on it._", ""]
    for i, it in enumerate(sl["items"], 1):
        L += [f"## {i}. {it['ticker']} — confidence {it['confidence']}", "",
              f"**What is unusual:** {it['whats_unusual']}", "",
              f"**Open interest:** {it['oi_confirmation']}. {it.get('oi_detail', '')}", "",
              f"**Sector comparison:** {it.get('sector_comparison', '')}", "",
              f"**Most likely boring explanation:** {it['boring_explanation']}"
              + ("" if it.get("boring_explanation_verified") else " _(unverified)_"), "",
              "**Research next:**"] + [f"- {r}" for r in it["research_next"]] + [
              "", f"**Confidence:** {it['confidence']} — {it['confidence_reason']}", ""]
    if sl.get("research_first_question"):
        L += ["---", f"**Research this first:** {sl['research_first_question']}"]
    return "\n".join(L) + "\n"


FLAG_LOG_COLS = ["run_date", "ticker", "rank", "confidence", "zscore", "ratio_to_median", "cp_today",
                 "oi_classification", "price_relationship", "sector_wide", "close_at_flag", "boring_explanation",
                 "close_after_10_sessions", "pct_change_10_sessions", "max_abs_move_pct_10_sessions", "outcome_notes"]


def append_flag_log(cfg: Config, sl: dict, a: dict) -> None:
    """Log every flag so a real hit rate can be measured later (see `review`)."""
    path = cfg.data_dir / "flag_log.csv"
    rows = list(csv.DictReader(path.open())) if path.exists() else []
    existing = {(r["run_date"], r["ticker"]) for r in rows}
    for i, it in enumerate(sl["items"], 1):
        if (sl["run_date"], it["ticker"]) in existing:
            continue
        m = a["metrics"][it["ticker"]]
        rows.append({"run_date": sl["run_date"], "ticker": it["ticker"], "rank": i, "confidence": it["confidence"],
                     "zscore": m["options_baseline"].get("zscore"),
                     "ratio_to_median": m["options_baseline"].get("ratio_to_median"),
                     "cp_today": m["cp_ratio"].get("today"), "oi_classification": m["oi"]["classification"],
                     "price_relationship": m["price_vs_options"]["relationship"],
                     "sector_wide": m.get("sector_context", {}).get("sector_wide"),
                     "close_at_flag": m["price"].get("last_close"),
                     "boring_explanation": it["boring_explanation"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:  # full rewrite keeps the header current if columns were added
        w = csv.DictWriter(fh, fieldnames=FLAG_LOG_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def review_flags(cfg: Config, sessions: int = 10) -> None:
    """Fill in what happened N sessions after each flag (from Yahoo daily bars)."""
    path = cfg.data_dir / "flag_log.csv"
    if not path.exists():
        print("No flag_log.csv yet.")
        return
    rows = list(csv.DictReader(path.open()))
    updated = 0
    for r in rows:
        if r.get("close_after_10_sessions") or not r.get("close_at_flag"):
            continue
        start = date.fromisoformat(r["run_date"])
        h = yf.Ticker(r["ticker"]).history(start=start.isoformat(),
                                           end=(start + timedelta(days=sessions * 2 + 10)).isoformat(),
                                           auto_adjust=False)
        h = h[[pd.Timestamp(i).date() > start for i in h.index]]
        if len(h) < sessions:
            continue
        c0 = float(r["close_at_flag"])
        win = h["Close"].iloc[:sessions]
        r["close_after_10_sessions"] = round(float(win.iloc[-1]), 4)
        r["pct_change_10_sessions"] = round((float(win.iloc[-1]) / c0 - 1) * 100, 2)
        r["max_abs_move_pct_10_sessions"] = round(float((win / c0 - 1).abs().max() * 100), 2)
        updated += 1
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FLAG_LOG_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Updated {updated} flag outcomes in {path}")


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run Data -> Analysis -> Flagging")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--tickers", nargs="+")
    g.add_argument("--watchlist", type=Path, help="text file, one ticker per line (# comments ok)")
    r.add_argument("--date", default=None, help="run date label YYYY-MM-DD (default: today, New York)")
    r.add_argument("--data-mode", choices=["agent", "direct"], default="agent",
                   help="agent = Claude drives the Yahoo tools; direct = same tools, no LLM")
    r.add_argument("--skip-data", action="store_true", help="reuse existing data file for --date")
    r.add_argument("--chunk-size", type=int, default=10, help="tickers per Analysis Agent call")
    r.add_argument("--max-expirations", type=int, default=None, help="cap chains fetched (totals become PARTIAL)")
    r.add_argument("--oi-missing", choices=["block", "warn"], default="block",
                   help="block (default): no flag without an OI increase; warn: allow flags with unavailable OI, capped LOW")
    r.add_argument("--benchmarks", type=Path, default=None,
                   help='JSON overrides: {"AAPL": {"sector_etf": "XLK", "peers": ["MSFT", "GOOGL"]}}')
    r.add_argument("--market-benchmark", default="SPY", help="market benchmark ticker, or 'none'")
    sub.add_parser("review", help="fill 10-session outcomes for logged flags")
    for p in (r, sub.choices["review"]):
        p.add_argument("--data-dir", type=Path, default=ROOT / "data")
        p.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(data_dir=args.data_dir)
    if args.cmd == "review":
        review_flags(cfg)
        return 0

    cfg.max_expirations = args.max_expirations
    cfg.oi_missing_policy = args.oi_missing
    cfg.benchmarks_file = args.benchmarks
    cfg.market_benchmark = None if args.market_benchmark.lower() == "none" else args.market_benchmark
    tickers = args.tickers or [ln.split("#")[0].strip() for ln in args.watchlist.read_text().splitlines()]
    tickers = list(dict.fromkeys(t.upper() for t in tickers if t))
    run_date = args.date or ny_today()
    if anthropic is None:
        raise SystemExit("pip install anthropic")
    client = anthropic.Anthropic()

    data_path = cfg.data_dir / run_date / f"data_agent_{run_date}.json"
    if args.skip_data:
        if not data_path.exists():
            raise SystemExit(f"--skip-data but {data_path} does not exist")
    else:
        data_path = run_data_agent(client, cfg, tickers, run_date, args.data_mode)
    analysis_path = run_analysis_agent(client, cfg, data_path, args.chunk_size)
    shortlist_path = run_flagging_agent(client, cfg, analysis_path)
    print("\n" + shortlist_path.read_text())
    print(f"Files: {data_path}\n       {analysis_path}\n       {shortlist_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
