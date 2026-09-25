"""Offline end-to-end test: fake Yahoo + fake Claude. Run: python -m pytest -q tests/  (or python tests/test_offline.py)"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestrator as o  # noqa: E402

SESSION = "2026-09-21"


class FakeTicker:
    def __init__(self, sym):
        self.sym = sym
        rng = np.random.default_rng(len(sym))
        idx = pd.bdate_range(end=SESSION, periods=120, tz="America/New_York")
        close = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx)))
        close[-1] = close[-2] * 1.001  # flat day -> NOT_MOVED
        self._hist = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                                   "Adj Close": close, "Volume": rng.integers(1e6, 2e6, len(idx))}, index=idx)
        ts = int(datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc).timestamp())
        self.info = {"regularMarketPrice": float(close[-1]), "regularMarketTime": ts, "marketState": "CLOSED",
                     "currency": "USD", "regularMarketVolume": int(self._hist["Volume"].iloc[-1])}
        if sym not in ("XLK", "SPY"):
            self.info["sector"] = "Technology"
        self.options = ("2026-10-16", "2026-11-20")
        self.calendar = {"Earnings Date": [pd.Timestamp("2026-10-28").date()],
                         "Ex-Dividend Date": pd.Timestamp("2026-09-30").date()}
        self.news = [{"content": {"title": f"{sym} headline", "pubDate": "2026-09-20T12:00:00Z",
                                  "provider": {"displayName": "Wire"}}}]

    def history(self, **_):
        return self._hist

    def option_chain(self, e):
        ltd = pd.Timestamp("2026-09-21 19:00", tz="UTC")
        big = 40000 if self.sym in ("HOT", "NEWB") else 900
        calls = pd.DataFrame({"contractSymbol": [f"{self.sym}{e}C{k}" for k in (95, 100, 105)],
                              "strike": [95, 100, 105], "lastTradeDate": [ltd] * 3,
                              "volume": [big, 500, np.nan], "openInterest": [30000, 4000, 100],
                              "impliedVolatility": [0.3] * 3})
        puts = pd.DataFrame({"contractSymbol": [f"{self.sym}{e}P{k}" for k in (95, 100)],
                             "strike": [95, 100], "lastTradeDate": [ltd, ltd - pd.Timedelta(days=3)],
                             "volume": [700, 999], "openInterest": [5000, 800], "impliedVolatility": [0.3] * 2})
        return SimpleNamespace(calls=calls, puts=puts)


def seed_history(cfg, sym):
    snaps = o.SnapshotStore(cfg.data_dir)
    for i, d in enumerate(pd.bdate_range(end="2026-09-18", periods=25)):
        s = d.date().isoformat()
        contracts = {f"{sym}{e}{k}": {"open_interest": oi, "volume_session": 100}
                     for e in ("2026-10-16", "2026-11-20")
                     for k, oi in (("C95", 20000), ("C100", 3000), ("C105", 100), ("P95", 4000), ("P100", 800))}
        snaps.save(sym, s, contracts, {"call_volume": 2000 + i * 10, "put_volume": 2000, "total_volume": 4000 + i * 10,
                                       "coverage_status": "OK"})


class Block(SimpleNamespace):
    pass


class FakeMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        tool = kw["tools"][0]["name"]
        data = json.loads(kw["messages"][0]["content"].split("\n\n", 1)[1])
        retry = len(kw["messages"]) > 1
        if tool == "submit_analysis":
            out = {"tickers": [], "ranked_flags": [], "run_caveats": []}
            for r in data["records"]:
                ds = r["metrics"]["deterministic_screens"]
                big = ds["volume_unusual_vs_own_history"] or ds["proxy_only_unusual"]
                out["tickers"].append({
                    "ticker": r["ticker"],
                    "options_volume_vs_normal": {"status": "OK", "zscore": 999, "description": "desc"},
                    "call_put_ratio": {"description": "desc"},
                    "open_interest": {"classification": "OPENED", "description": "desc"},
                    "price_vs_options": {"price_move": "FLAT", "options_tilt": "CALL-TILTED",
                                         "relationship": "NOT_MOVED", "description": "desc"},
                    "scheduled_events": {"could_explain": False, "description": "desc"},
                    "sector_context": {"sector_wide": "NO", "description": "desc"},
                    "flagged": big, "flag_basis": "basis" if retry else "looks bullish",  # ignores OI policy on purpose
                    "cannot_determine": ["a", "b", "c", "d"]})
        else:
            tick = data["ranked_flags"] + (["NEWB"] if "NEWB" not in data["ranked_flags"] else [])
            items = [{"ticker": t, "whats_unusual": "numbers", "oi_confirmation": "YES — opened",
                      "sector_comparison": "XLK normal",
                      "boring_explanation": "earnings", "research_next": ["check OI tomorrow"],
                      "confidence": "HIGH", "confidence_reason": "reason"} for t in tick]
            items.append(dict(items[0], ticker="FAKE")) if items else None
            out = {"nothing_unusual": not items, "items": items, "research_first_question": "Why?"}
        return SimpleNamespace(stop_reason="tool_use",
                               content=[Block(type="tool_use", id=f"t{self.calls}", name=tool, input=out)])


def _run(policy):
    o.yf = SimpleNamespace(Ticker=FakeTicker, __version__="fake")
    d = tempfile.mkdtemp()
    cfg = o.Config(data_dir=Path(d), oi_missing_policy=policy)
    bf = Path(d) / "benchmarks.json"
    bf.write_text(json.dumps({"CALM": {"peers": ["HOT"]}}))
    cfg.benchmarks_file = bf
    for s in ("HOT", "CALM", "XLK", "SPY"):
        seed_history(cfg, s)
    client = SimpleNamespace(messages=FakeMessages())
    data_path = o.run_data_agent(client, cfg, ["HOT", "CALM", "NEWB"], "2026-09-22", "direct")
    a_path = o.run_analysis_agent(client, cfg, data_path, chunk_size=2)
    s_path = o.run_flagging_agent(client, cfg, a_path)
    return cfg, data_path, a_path, s_path


def test_block_policy_and_sector():
    cfg, data_path, a_path, s_path = _run("block")
    data = json.loads(data_path.read_text())
    assert set(data["benchmark_tickers"]) == {"XLK", "SPY"}
    assert data["benchmark_map"]["HOT"]["sector_etf"] == "XLK"
    hot = data["records"]["HOT"]["fields"]
    assert hot["options_volume_today"]["value"]["contracts_with_earlier_session_volume_excluded"] == 2
    assert hot["oi_change_active_strikes"]["status"] == "OK"
    assert all("source_url" in v and "fetched_at_utc" in v for v in hot.values())

    a = json.loads(a_path.read_text())
    e = {x["ticker"]: x for x in a["tickers"]}
    assert e["HOT"]["options_volume_vs_normal"]["zscore"] != 999          # number overwritten by computed
    assert "buy" in e["HOT"]["cannot_determine"][0].lower()               # standard caveats injected
    assert a["ranked_flags"] == ["HOT"]                                    # NEWB unflagged: OI unavailable
    assert any("unflagged" in c for c in e["NEWB"]["data_caveats"])
    assert a["metrics"]["HOT"]["sector_context"]["sector_wide"] == "NO"   # XLK normal
    assert a["metrics"]["CALM"]["sector_context"]["sector_wide"] == "YES" # peer HOT unusual
    assert e["CALM"]["sector_context"]["sector_wide"] == "YES"            # agent's "NO" overwritten
    assert not o.lint(a["tickers"], o.BASE_BANNED)                         # lint retry removed "bullish"

    sl = json.loads(s_path.with_suffix(".json").read_text())
    tick = [i["ticker"] for i in sl["items"]]
    assert tick == ["HOT"]                                                 # FAKE + NEWB dropped
    assert sl["items"][0]["confidence"] == "MEDIUM"                        # capped (ex-div in window)
    assert "sector_wide" in (cfg.data_dir / "flag_log.csv").read_text().splitlines()[0]
    print(s_path.read_text())
    print(a_path.with_suffix(".md").read_text())


def test_warn_policy():
    _, _, a_path, s_path = _run("warn")
    a = json.loads(a_path.read_text())
    assert "NEWB" in a["ranked_flags"] and a["ranked_flags"][0] == "HOT"   # proxy-only ranks below
    sl = json.loads(s_path.with_suffix(".json").read_text())
    newb = next(i for i in sl["items"] if i["ticker"] == "NEWB")
    assert newb["confidence"] == "LOW"


def test_nothing_unusual_single_line():
    sl = {"nothing_unusual": True, "items": [], "run_date": SESSION}
    assert o.render_shortlist_md(sl, 12).count("\n") == 1


if __name__ == "__main__":
    test_block_policy_and_sector()
    test_warn_policy()
    test_nothing_unusual_single_line()
    print("OK")
