"""
AI Trading Bot — Streamlit Dashboard
=====================================
Run locally:  streamlit run dashboard.py
Deploy:       Push to GitHub, connect repo to Streamlit Cloud
              Set ANTHROPIC_API_KEY in Streamlit Cloud → App Settings → Secrets
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="AI Trading Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Import pipeline ───────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
try:
    from pipeline import (
        fetch_market_data,
        build_research_data_parallel,
        PortfolioManager,
        SkillRunner,
        MockRobinhoodEngine,
        load_knowledge_base,
        load_brier_log,
        load_trade_history,
        kill_switch_active,
        step1_scan,
        step2_research,
        step3_predict,
        step4_execute,
        EXECUTION_CONFIG,
        BASE_DIR,
        PORTFOLIO_FILE,
    )
    PIPELINE_AVAILABLE = True
except ImportError as e:
    PIPELINE_AVAILABLE = False
    IMPORT_ERROR = str(e)


# ── Helpers ───────────────────────────────────────────────────────────────────

BIAS_COLORS      = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}
EDGE_COLORS      = {"STRONG": "🟢", "MODERATE": "🟡", "WEAK": "🔴"}


def score_bar(value: float, max_val: float = 100, color: str = "#2563eb") -> str:
    pct = min(max(value / max_val * 100, 0), 100)
    return (
        f'<div style="background:#e5e7eb;border-radius:4px;height:8px;width:100%">'
        f'<div style="background:{color};width:{pct:.0f}%;height:8px;border-radius:4px"></div>'
        f'</div><small>{value:.1f}</small>'
    )


def fmt_pct(v) -> str:
    return "—" if v is None else f"{v:.1%}"


def fmt_usd(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


def fmt_ts(iso: str, label: str = "") -> str:
    """Convert ISO-8601 UTC string to a readable label."""
    if not iso:
        return "—"
    return iso[:19].replace("T", " ") + " UTC"


def generate_interpretation(client, results: dict) -> str:
    summary_json = json.dumps(results, indent=2, default=str)[:6000]
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=(
            "You are a clear, direct financial analyst writing a brief debrief "
            "for a non-technical user of an AI stock trading bot. "
            "Be specific about tickers, numbers, and what they mean in plain English. "
            "Do not use bullet point lists — write in short paragraphs. "
            "Maximum 250 words."
        ),
        messages=[{
            "role": "user",
            "content": (
                "Here are the results of a full AI trading pipeline scan. "
                "Summarise: what was scanned, what research found, which signals (if any) "
                "passed the edge threshold, and whether any mock trades were executed. "
                "End with one sentence on what to watch next.\n\n"
                f"Results:\n{summary_json}"
            ),
        }],
    )
    return resp.content[0].text.strip()


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📈 AI Trading Bot")
    st.caption("Paper trading · $100k virtual portfolio")
    st.divider()

    # API key — Streamlit secrets (Cloud) → env var → sidebar input
    default_key = ""
    try:
        default_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    except Exception:
        pass
    if not default_key:
        default_key = os.environ.get("ANTHROPIC_API_KEY", "")

    api_key = st.text_input(
        "Anthropic API Key",
        value=default_key,
        type="password",
        placeholder="sk-ant-...",
        help="Get your key at console.anthropic.com. On Streamlit Cloud, set it in App Secrets.",
    )

    tickers_raw = st.text_input(
        "Tickers (comma-separated)",
        value="AAPL, TSLA, NVDA, AMD, MSFT",
        placeholder="AAPL, TSLA, NVDA, AMD",
        help="Enter any US stock tickers. More tickers = better chance of finding an edge signal.",
    )

    st.divider()
    run_btn = st.button("▶ Run Analysis", type="primary", use_container_width=True)

    # Live portfolio snapshot
    st.divider()
    st.subheader("💼 Portfolio")
    if PIPELINE_AVAILABLE and PORTFOLIO_FILE.exists():
        pf   = PortfolioManager(PORTFOLIO_FILE).snapshot()
        peak = pf.get("peak_total_value", 100_000)
        dd   = (peak - pf["total_value"]) / peak if peak > 0 else 0
        st.metric("Total Value",    fmt_usd(pf["total_value"]),
                  delta=fmt_usd(pf["all_time_pnl"]))
        st.metric("Cash",           fmt_usd(pf["cash_balance"]))
        st.metric("Open Positions", len(pf.get("open_positions", [])))
        st.metric("Daily P&L",      fmt_usd(pf["daily_pnl"]))
        if dd > 0:
            st.metric("Drawdown", fmt_pct(dd), delta_color="inverse")
        if kill_switch_active():
            st.error("⛔ Kill switch active")
    else:
        st.caption("No portfolio yet — run the bot to start.")

    st.divider()
    st.caption("Built with Claude · yfinance · Streamlit")


# ── Main ──────────────────────────────────────────────────────────────────────

st.title("📊 AI Trading Pipeline Dashboard")

if not PIPELINE_AVAILABLE:
    st.error(f"Could not import pipeline.py: `{IMPORT_ERROR}`\n\n"
             "Make sure `pipeline.py` is in the same folder as `dashboard.py`.")
    st.stop()

# Session state
for key in ["scan", "research", "predict", "execution", "interpretation", "ran"]:
    if key not in st.session_state:
        st.session_state[key] = None

# ── Run block ─────────────────────────────────────────────────────────────────
if run_btn:
    if not api_key or not api_key.startswith("sk-"):
        st.error("Please enter a valid Anthropic API key (starts with `sk-ant-...`).")
        st.stop()

    tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]
    if not tickers:
        st.error("Please enter at least one ticker symbol.")
        st.stop()

    # Inject API key so SkillRunner picks it up
    os.environ["ANTHROPIC_API_KEY"] = api_key
    import anthropic
    runner    = SkillRunner()
    client    = anthropic.Anthropic(api_key=api_key)
    portfolio = PortfolioManager(PORTFOLIO_FILE)

    # Step 1 — uses step1_scan() from pipeline so all timestamp/data-range logic applies
    with st.status("⚙️ Step 1 — Fetching market data & scanning...", expanded=True) as s1:
        st.write(f"Pulling 30-day yfinance data for: **{', '.join(tickers)}**")
        market_data = fetch_market_data(tickers)
        if not market_data:
            st.error("Failed to fetch market data. Check your ticker symbols.")
            st.stop()
        dr = market_data[0].get("_data_range_start", "?") + " → " + \
             market_data[0].get("_data_range_end", "?")
        st.write(f"📅 Data window: **{dr}** · source: Yahoo Finance (live)")
        scan = step1_scan(runner, tickers)
        st.session_state.scan = scan
        n_passed = scan.get("total_passed", len(scan.get("tickers", [])))
        s1.update(
            label=f"✅ Step 1 — {n_passed}/{len(tickers)} tickers passed · data: {dr}",
            state="complete",
        )

    # Step 2 — uses step2_research() so current_utc_time + timestamp override apply
    symbols = [t["symbol"] for t in scan.get("tickers", [])]
    with st.status("⚙️ Step 2 — Multi-source intelligence gathering...", expanded=True) as s2:
        st.write(f"Scraping Yahoo Finance, Reddit, RSS in parallel for: **{', '.join(symbols)}**")
        research = step2_research(runner, scan)
        st.session_state.research = research
        n_briefs = len(research.get("briefs", []))
        ts2 = fmt_ts(research.get("research_timestamp", ""))
        s2.update(label=f"✅ Step 2 — {n_briefs} briefs produced · {ts2}", state="complete")

    # Step 3 — uses step3_predict() so ensemble + timestamps apply
    with st.status("⚙️ Step 3 — Ensemble probability prediction...", expanded=True) as s3:
        st.write("5-model ensemble vote · EV · Z-score · 4% edge gate")
        predict = step3_predict(runner, scan, research)
        st.session_state.predict = predict
        passed  = predict.get("summary", {}).get("passed_gate", 0)
        blocked = predict.get("summary", {}).get("blocked_gate", 0)
        ts3 = fmt_ts(predict.get("predict_timestamp", ""))
        s3.update(
            label=f"✅ Step 3 — {passed} passed gate, {blocked} blocked · {ts3}",
            state="complete",
        )

    # Step 4 — uses step4_execute()
    with st.status("⚙️ Step 4 — Risk checks & mock execution...", expanded=True) as s4:
        st.write("Fractional Kelly · VaR · drawdown circuit breaker · mock Robinhood limit orders")
        execution = step4_execute(predict, portfolio)
        st.session_state.execution = execution
        n_orders = len(execution.get("orders", []))
        ts4 = fmt_ts(execution.get("execution_timestamp", ""))
        s4.update(label=f"✅ Step 4 — {n_orders} order(s) placed · {ts4}", state="complete")

    # AI interpretation
    with st.status("⚙️ Generating AI interpretation...", expanded=True) as si:
        results_summary = {
            "data_window": {
                "start":  scan.get("data_range_start"),
                "end":    scan.get("data_range_end"),
                "source": scan.get("data_source"),
            },
            "scan":      scan.get("tickers", []),
            "predict":   predict.get("signals", []),
            "execution": {
                "orders":            execution.get("orders", []),
                "portfolio_summary": execution.get("portfolio_summary", {}),
            },
        }
        interp = generate_interpretation(client, results_summary)
        st.session_state.interpretation = interp
        st.session_state.ran = True
        si.update(label="✅ Interpretation ready", state="complete")


# ── Results display ───────────────────────────────────────────────────────────
if st.session_state.ran:
    scan      = st.session_state.scan
    research  = st.session_state.research
    predict   = st.session_state.predict
    execution = st.session_state.execution
    interp    = st.session_state.interpretation

    # ── Top info bar — timestamps + data window ───────────────────────────────
    dr_start = scan.get("data_range_start", "—")
    dr_end   = scan.get("data_range_end",   "—")
    scan_ts  = fmt_ts(scan.get("scan_timestamp", ""))
    res_ts   = fmt_ts(research.get("research_timestamp", ""))
    pred_ts  = fmt_ts(predict.get("predict_timestamp", ""))
    exec_ts  = fmt_ts(execution.get("execution_timestamp", ""))

    with st.container(border=True):
        st.markdown("#### 🕐 Pipeline Run Timestamps & Data Window")
        ti1, ti2, ti3, ti4 = st.columns(4)
        ti1.metric("📅 Data Window",      f"{dr_start} → {dr_end}",
                   help="Actual trading days fetched from Yahoo Finance via yfinance")
        ti2.metric("🔍 Scanned At",       scan_ts,
                   help="Wall-clock time Step 1 completed")
        ti3.metric("📰 Researched At",    res_ts,
                   help="Wall-clock time Step 2 completed")
        ti4.metric("🎯 Predicted At",     pred_ts,
                   help="Wall-clock time Step 3 completed")

        ti5, ti6, ti7, ti8 = st.columns(4)
        ti5.metric("⚡ Executed At",      exec_ts,
                   help="Wall-clock time Step 4 completed")
        ti6.metric("📡 Data Source",      "Yahoo Finance (live)",
                   help="All price/volume data fetched live via yfinance at run time")
        ti7.metric("🗓️ Trading Days",     f"~{len(scan.get('tickers', [])) * 21}",
                   help="Approx. trading day records loaded (~21 per ticker over 30 calendar days)")
        ti8.metric("💱 Tickers Scanned",  scan.get("total_scanned", len(scan.get("tickers", []))))

    st.divider()

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "🔍 Step 1 · Scan",
        "📰 Step 2 · Research",
        "🎯 Step 3 · Predict",
        "⚡ Step 4 · Execute",
        "🧠 Summary",
    ])

    # ── TAB 1: SCAN ───────────────────────────────────────────────────────────
    with tab1:
        st.subheader("Market Scanner Results")
        tickers_data = scan.get("tickers", [])

        st.info(
            f"📅 **Market data window:** {dr_start} → {dr_end} &nbsp;|&nbsp; "
            f"Source: {scan.get('data_source', 'yfinance · Yahoo Finance (live)')} &nbsp;|&nbsp; "
            f"Scanned at: {scan_ts}"
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tickers Scanned",  scan.get("total_scanned", len(tickers_data)))
        c2.metric("Passed Filters",   scan.get("total_passed",  len(tickers_data)))
        c3.metric("Data Start",       dr_start)
        c4.metric("Data End",         dr_end)

        st.divider()

        for t in tickers_data:
            bias  = t.get("bias", "NEUTRAL")
            icon  = BIAS_COLORS.get(bias, "🟡")
            score = t.get("opportunity_score", 0)
            flags = t.get("anomaly_flags", [])
            t_start = t.get("data_date_start", dr_start)
            t_end   = t.get("data_date_end",   dr_end)

            with st.container(border=True):
                col_a, col_b, col_c, col_d = st.columns([2, 3, 3, 4])

                with col_a:
                    st.markdown(f"### {t['symbol']}")
                    st.markdown(f"{icon} **{bias}**")
                    st.caption(f"Data: {t_start} → {t_end}")

                with col_b:
                    st.metric("Last Price",   fmt_usd(t.get("last_price")))
                    st.metric("Volume Ratio", f"{t.get('volume_ratio', 0):.2f}×")

                with col_c:
                    st.metric("RSI (14)",   f"{t.get('rsi_14', 0):.1f}")
                    st.metric("1d Change",  f"{t.get('price_change_1d_pct', 0):+.2f}%")

                with col_d:
                    st.markdown("**Opportunity Score**")
                    bar_color = ("#2563eb" if score > 60
                                 else "#f59e0b" if score > 40
                                 else "#dc2626")
                    st.markdown(score_bar(score, 100, bar_color), unsafe_allow_html=True)
                    if flags:
                        st.markdown(
                            " ".join(f'<code style="font-size:11px">{f}</code>' for f in flags),
                            unsafe_allow_html=True,
                        )

        with st.expander("Raw JSON — Step 1"):
            st.json(scan)

    # ── TAB 2: RESEARCH ───────────────────────────────────────────────────────
    with tab2:
        st.subheader("Multi-Source Intelligence Briefs")
        st.caption(f"Researched at: {res_ts}")
        briefs = research.get("briefs", [])

        for brief in briefs:
            sym       = brief.get("symbol", "")
            sent      = brief.get("sentiment", {})
            gap_obj   = brief.get("narrative_gap", {})
            sources   = brief.get("sources_available", [])
            consensus = sent.get("narrative_consensus", "NEUTRAL")
            conf      = sent.get("narrative_confidence", 0)
            gap       = gap_obj.get("narrative_gap", 0)
            edge_sig  = gap_obj.get("trading_edge_signal", "WEAK")
            bull_score= sent.get("weighted_bull_score", 0.5)

            with st.expander(
                f"{BIAS_COLORS.get(consensus, '🟡')} {sym} — "
                f"{consensus} · Edge Signal: {EDGE_COLORS.get(edge_sig, '🔴')} {edge_sig}",
                expanded=True,
            ):
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("Sentiment",     consensus)
                r2.metric("Confidence",    fmt_pct(conf))
                r3.metric("Bull Score",    f"{bull_score:.2f}")
                r4.metric("Narrative Gap", f"{gap:+.3f}",
                          delta_color="normal" if gap >= 0 else "inverse",
                          help="Positive = sources more bullish than price implies → potential upside")

                st.markdown(f"**Sources used:** {', '.join(sources) if sources else 'none'}")

                yahoo   = brief.get("yahoo", {})
                analyst = yahoo.get("analyst_ratings", {})
                if analyst.get("consensus_rating"):
                    ac1, ac2, ac3 = st.columns(3)
                    ac1.metric("Analyst Rating", analyst.get("consensus_rating", "—"))
                    ac2.metric("Price Target",   fmt_usd(analyst.get("mean_price_target")))
                    ac3.metric("# Analysts",     analyst.get("num_analysts", 0))

                drivers = sent.get("key_drivers", [])
                if drivers:
                    st.markdown("**Key drivers:** " + " · ".join(f"`{d}`" for d in drivers))

                news = yahoo.get("news", [])
                if news:
                    st.markdown("**Recent headlines:**")
                    for article in news[:3]:
                        if not article.get("injection_attempt_detected"):
                            st.markdown(f"- {article.get('headline', '')}")

        with st.expander("Raw JSON — Step 2"):
            st.json(research)

    # ── TAB 3: PREDICT ────────────────────────────────────────────────────────
    with tab3:
        st.subheader("Ensemble Prediction & Edge Gate")
        st.caption(f"Predicted at: {pred_ts}")
        signals = predict.get("signals", [])
        summ    = predict.get("summary", {})

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Evaluated",   summ.get("total_evaluated", len(signals)))
        m2.metric("Passed Gate", summ.get("passed_gate", 0))
        m3.metric("Blocked",     summ.get("blocked_gate", 0))
        brier = summ.get("running_brier_score")
        m4.metric("Brier Score", f"{brier:.3f}" if brier else "No history",
                  help="Prediction calibration. < 0.25 = well-calibrated. Lower is better.")

        st.divider()

        passed_sigs  = [s for s in signals if s.get("pass_to_execution")]
        blocked_sigs = [s for s in signals if not s.get("pass_to_execution")]

        if passed_sigs:
            st.markdown("### ✅ Signals That Passed the Gate")
            for sig in passed_sigs:
                ens  = sig.get("ensemble", {})
                comp = sig.get("components", {})
                with st.container(border=True):
                    h1, h2, h3, h4, h5 = st.columns(5)
                    h1.metric("Symbol",      sig["symbol"])
                    h2.metric("Action",      sig.get("recommended_action", "—"))
                    h3.metric("Edge",        f"{sig.get('edge_pct', 0):.1f}%")
                    h4.metric("EV",          f"{sig.get('EV', 0):.3f}")
                    h5.metric("Probability", fmt_pct(sig.get("directional_probability")))

                    st.markdown("**Ensemble model votes (5 independent models):**")
                    em1, em2, em3, em4, em5 = st.columns(5)
                    for col, label, key in zip(
                        [em1, em2, em3, em4, em5],
                        ["Technical", "Sentiment", "Analyst", "Gap", "Anomaly"],
                        ["p_technical", "p_sentiment", "p_analyst", "p_gap", "p_anomaly"],
                    ):
                        val = ens.get(key, 0)
                        color = "#2563eb" if val >= 0.55 else "#6b7280"
                        col.markdown(
                            f"**{label}**<br>"
                            + score_bar(val * 100, 100, color),
                            unsafe_allow_html=True,
                        )

                    st.markdown("**Mispricing score components:**")
                    sc1, sc2, sc3, sc4 = st.columns(4)
                    for col, (label, val) in zip(
                        [sc1, sc2, sc3, sc4],
                        [("Technical", comp.get("technical", 50)),
                         ("Sentiment", comp.get("sentiment", 50)),
                         ("Analyst",   comp.get("analyst",   50)),
                         ("Gap",       comp.get("gap",       50))],
                    ):
                        col.markdown(
                            f"**{label}**<br>" + score_bar(val, 100),
                            unsafe_allow_html=True,
                        )

                    if sig.get("model_dissent"):
                        st.warning("⚠️ Model dissent detected — one or more models strongly disagree.")

        if blocked_sigs:
            st.markdown("### 🚫 Blocked Signals")
            rows = [{
                "Symbol":      s["symbol"],
                "Bias":        s.get("bias", "—"),
                "Edge %":      f"{s.get('edge_pct', 0):.2f}%",
                "Probability": fmt_pct(s.get("directional_probability")),
                "Mispricing":  f"{s.get('mispricing_score', 0):.1f}",
                "Reason":      s.get("gate_fail_reason", "—"),
            } for s in blocked_sigs]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        with st.expander("Raw JSON — Step 3"):
            st.json(predict)

    # ── TAB 4: EXECUTE ────────────────────────────────────────────────────────
    with tab4:
        st.subheader("Risk Management & Mock Execution")
        st.caption(f"Executed at: {exec_ts}")

        if execution.get("kill_switch_active"):
            st.error("⛔ Kill switch is active — no trades executed.")
        else:
            orders   = execution.get("orders", [])
            rejected = execution.get("rejected_signals", [])
            pf       = execution.get("portfolio_summary", {})

            e1, e2, e3, e4 = st.columns(4)
            e1.metric("Orders Executed", len(orders))
            e2.metric("Risk Rejections", len(rejected))
            e3.metric("Cash Balance",    fmt_usd(pf.get("cash_balance")))
            e4.metric("Total Value",     fmt_usd(pf.get("total_value")))

            if orders:
                st.markdown("### 📋 Executed Orders")
                for o in orders:
                    op  = o.get("order_payload", {})
                    pd_ = o.get("portfolio_delta", {})
                    with st.container(border=True):
                        oc1, oc2, oc3, oc4, oc5 = st.columns(5)
                        oc1.metric("Symbol",      op.get("symbol", "—"))
                        oc2.metric("Side",        op.get("side", "—").upper())
                        oc3.metric("Shares",      op.get("quantity", "—"))
                        oc4.metric("Limit Price", fmt_usd(op.get("limit_price")))
                        oc5.metric("Cost",        fmt_usd(pd_.get("cash_deducted")))

                        oi1, oi2, oi3 = st.columns(3)
                        oi1.metric("Stop Loss",    fmt_usd(op.get("stop_loss_price")))
                        oi2.metric("Take Profit",  fmt_usd(op.get("take_profit_price")))
                        oi3.metric("Position VaR", fmt_usd(op.get("position_var_95")))

                        st.caption(
                            f"Kelly: {op.get('kelly_fraction_used', 0):.0%} (quarter-Kelly) · "
                            f"Full Kelly: {op.get('full_kelly', 0):.3f} · "
                            f"Scaled Kelly: {op.get('scaled_kelly', 0):.3f} · "
                            f"ID: `{op.get('client_id', '—')}`"
                        )
            elif not rejected:
                st.info(
                    "No signals cleared the prediction gate — nothing to execute. "
                    "Try more tickers or run during higher-volatility sessions."
                )

            if rejected:
                st.markdown("### 🚫 Risk Rejections")
                for r in rejected:
                    st.warning(f"**{r['symbol']}** — {r['rejection_reason']}")

            st.divider()
            st.markdown("### 💼 Portfolio After Run")
            open_pos = pf.get("open_positions", [])
            if not isinstance(open_pos, list):
                open_pos = []
            peak     = pf.get("peak_total_value", EXECUTION_CONFIG["initial_capital"])
            total    = pf.get("total_value", 0)
            drawdown = (peak - total) / peak if peak > 0 else 0

            pfc1, pfc2, pfc3, pfc4 = st.columns(4)
            pfc1.metric("Cash",         fmt_usd(pf.get("cash_balance")))
            pfc2.metric("Equity",       fmt_usd(pf.get("equity_value")))
            pfc3.metric("Open Pos.",    len(open_pos))
            pfc4.metric("Max Drawdown", fmt_pct(drawdown))

            if open_pos:
                pos_rows = [{
                    "Symbol":      p["symbol"],
                    "Side":        p["side"],
                    "Shares":      p["shares"],
                    "Entry Price": fmt_usd(p.get("entry_price")),
                    "Cost Basis":  fmt_usd(p.get("cost_basis")),
                    "Stop Loss":   fmt_usd(p.get("stop_loss_price")),
                    "Take Profit": fmt_usd(p.get("take_profit_price")),
                    "Unreal. P&L": fmt_usd(p.get("unrealised_pnl", 0)),
                } for p in open_pos]
                st.dataframe(pd.DataFrame(pos_rows), use_container_width=True, hide_index=True)

        with st.expander("Raw JSON — Step 4"):
            st.json(execution)

    # ── TAB 5: SUMMARY ────────────────────────────────────────────────────────
    with tab5:
        st.subheader("🧠 AI Interpretation")
        st.info(interp)

        st.divider()
        st.subheader("📈 Trade History")
        history = load_trade_history()
        if history:
            hist_rows = [{
                "Symbol":      t.get("symbol"),
                "Side":        t.get("side"),
                "Shares":      t.get("shares"),
                "Entry":       fmt_usd(t.get("entry_price")),
                "Exit":        fmt_usd(t.get("exit_price")),
                "P&L":         fmt_usd(t.get("realised_pnl")),
                "Exit Reason": t.get("exit_reason", "—"),
                "Date":        (t.get("exit_time") or "")[:10],
            } for t in history[-20:]]
            st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, hide_index=True)

            wins      = sum(1 for t in history if t.get("realised_pnl", 0) > 0)
            total_pnl = sum(t.get("realised_pnl", 0) for t in history)
            sh1, sh2, sh3, sh4 = st.columns(4)
            sh1.metric("Total Trades",  len(history))
            sh2.metric("Win Rate",      fmt_pct(wins / len(history)))
            sh3.metric("Wins / Losses", f"{wins} / {len(history) - wins}")
            sh4.metric("All-Time P&L",  fmt_usd(total_pnl))
        else:
            st.caption("No closed trades yet.")

        st.divider()
        kb = load_knowledge_base()
        if kb.get("lessons"):
            st.subheader("📚 Knowledge Base — Recent Lessons")
            for lesson in kb["lessons"][-5:]:
                st.markdown(
                    f"**{lesson.get('symbol', '—')}** · "
                    f"`{lesson.get('failure_type', '—')}` — "
                    f"{lesson.get('lesson', '')}"
                )

else:
    # ── Landing state ─────────────────────────────────────────────────────────
    st.markdown("""
    ### How to use this dashboard

    1. Enter your **Anthropic API key** in the sidebar
    2. Type the **ticker symbols** you want to scan, separated by commas
    3. Click **▶ Run Analysis**

    The bot runs all 4 pipeline steps and displays live results:

    | Step | What it does |
    |------|-------------|
    | 🔍 Scan | Fetches 30 days of live yfinance data · filters by liquidity, volume, RSI · scores each ticker |
    | 📰 Research | Scrapes Yahoo Finance, Reddit, RSS in parallel · sentiment analysis · narrative gap |
    | 🎯 Predict | 5-model ensemble vote → EV + Z-score → 4% edge gate |
    | ⚡ Execute | Fractional Kelly sizing · VaR check · mock Robinhood limit order |

    **Timestamps shown for every step** — Scanned At, Researched At, Predicted At, Executed At —
    all set by Python's real clock, never by the AI model.

    **Data window** (e.g. `2026-05-12 → 2026-06-10`) shows the actual trading days
    fetched live from Yahoo Finance — always current, never from training data.

    ---
    💡 **Tip:** Start with 5–10 tickers (~$0.10–0.20 in API cost, ~2 min).
    Full 70-ticker scan costs ~$0.50–1.00 and takes 5–10 minutes.
    """)
