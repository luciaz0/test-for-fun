#!/usr/bin/env python3
"""
Streamlit dashboard for the options screener.

Reads the same CSVs that `dashboard.py` builds from data/<date>/*.json:
  data/run_log.csv      one row per day: completion time, tickers run, flags found, caveats
  data/ticker_log.csv   one row per ticker per day: volume, C/P ratio, z-score, OI status, price move

It does not call Yahoo or Claude and does not run the screener itself — run the pipeline first:
    python orchestrator.py run --watchlist watchlist.txt
    python dashboard.py                       # (re)builds the CSVs this app reads
    streamlit run streamlit_app.py
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PT = ZoneInfo("America/Los_Angeles")  # displays as PST or PDT depending on the date

st.set_page_config(page_title="Stock Performance Analysis", layout="wide")


def to_pt_display(utc_iso: str) -> str:
    """Convert a stored UTC ISO timestamp to a human-readable Pacific-time string.
    Underlying CSVs keep UTC (portable, unambiguous); only the UI converts to PT."""
    if not utc_iso or pd.isna(utc_iso):
        return ""
    try:
        dt = datetime.fromisoformat(str(utc_iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PT).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except (ValueError, TypeError):
        return str(utc_iso)


@st.cache_data(ttl=60)
def load_csv(name: str) -> pd.DataFrame:
    path = DATA_DIR / name
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def main() -> None:
    st.title("📊 Stock Performance Analysis — daily dashboard")
    st.caption(
        "Descriptive only, not a recommendation. Source: Yahoo Finance (delayed), via the 3-agent "
        "screener pipeline (`orchestrator.py`). Data refreshes from `data/run_log.csv` / "
        "`data/ticker_log.csv` — rebuild those with `python dashboard.py` after each daily run."
    )

    run_df = load_csv("run_log.csv")
    ticker_df = load_csv("ticker_log.csv")

    if run_df.empty:
        st.warning(
            "No runs logged yet. Run `python orchestrator.py run --watchlist watchlist.txt`, "
            "then `python dashboard.py` to build the CSVs this app reads."
        )
        st.stop()

    run_df = run_df.sort_values("run_date")
    ticker_df = ticker_df.sort_values(["run_date", "ticker"])

    latest_date = run_df["run_date"].iloc[-1]
    latest_run = run_df[run_df["run_date"] == latest_date].iloc[0]
    latest_tickers = ticker_df[ticker_df["run_date"] == latest_date].copy()

    st.subheader(f"Latest run — {latest_date}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tickers screened", int(latest_run["ticker_count"]))
    c2.metric("Flags", int(latest_run["flags_count"]))
    c3.metric("Total runs logged", len(run_df))
    c4.metric("Completed (PT)", to_pt_display(latest_run["completed_at_utc"]))

    if latest_run["flags_count"] > 0:
        st.success(f"Flagged tickers: {latest_run['flagged_tickers']}")
    else:
        st.info("No genuinely unusual options activity flagged in the latest run.")

    with st.expander("Run caveats (latest run)"):
        for c in str(latest_run.get("run_caveats", "")).split(" | "):
            if c:
                st.write(f"- {c}")

    st.divider()
    st.subheader("Run history")
    run_display = run_df.copy()
    run_display["completed_at_pt"] = run_display["completed_at_utc"].apply(to_pt_display)
    st.dataframe(
        run_display[["run_date", "completed_at_pt", "ticker_count", "flags_count", "flagged_tickers",
                     "data_mode", "oi_missing_policy"]].rename(columns={"completed_at_pt": "Completed (PT)"}),
        use_container_width=True, hide_index=True,
    )

    st.divider()
    st.subheader(f"Latest run detail — {latest_date}")
    show_cols = ["ticker", "options_volume_total", "calls", "puts", "cp_ratio",
                 "options_baseline_status", "zscore", "oi_classification",
                 "price_chg_1d_pct", "price_move_label", "sector_wide", "flagged"]
    st.dataframe(latest_tickers[show_cols], use_container_width=True, hide_index=True)

    fig_vol = px.bar(latest_tickers.sort_values("options_volume_total", ascending=False),
                      x="ticker", y="options_volume_total", color="flagged",
                      title="Today's options volume by ticker",
                      labels={"options_volume_total": "Options volume", "ticker": "Ticker"})
    st.plotly_chart(fig_vol, use_container_width=True)

    st.divider()
    st.subheader("Per-ticker trend over time")
    all_tickers = sorted(ticker_df["ticker"].unique())
    picked = st.multiselect("Tickers to chart", all_tickers, default=all_tickers)
    trend = ticker_df[ticker_df["ticker"].isin(picked)]

    if not trend.empty:
        fig1 = px.line(trend, x="run_date", y="options_volume_total", color="ticker", markers=True,
                        title="Options volume over time")
        st.plotly_chart(fig1, use_container_width=True)

        fig2 = px.line(trend, x="run_date", y="cp_ratio", color="ticker", markers=True,
                        title="Call/Put ratio over time")
        st.plotly_chart(fig2, use_container_width=True)

        fig3 = px.line(trend, x="run_date", y="price_chg_1d_pct", color="ticker", markers=True,
                        title="1-day price change (%) over time")
        st.plotly_chart(fig3, use_container_width=True)

        if trend["zscore"].notna().any():
            fig4 = px.line(trend.dropna(subset=["zscore"]), x="run_date", y="zscore", color="ticker",
                            markers=True, title="Options-volume z-score vs own history (available after ~20 sessions)")
            st.plotly_chart(fig4, use_container_width=True)
        else:
            st.caption("z-score chart will appear once a ticker has ≥20 sessions of history "
                       "(INSUFFICIENT_HISTORY until then).")

    st.divider()
    with st.expander("Raw ticker_log.csv"):
        st.dataframe(ticker_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
