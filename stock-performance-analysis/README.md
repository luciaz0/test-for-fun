# Options-activity screener (3 Claude agents)

```
watchlist ─► Data Agent ─► data/<date>/data_agent_<date>.json      (Yahoo Finance only, sourced + timestamped;
              │                                                        watchlist + sector ETF / peers / SPY)
              │  └─► data/history/options/<TICKER>/<session>.*.json (daily snapshots → OI change + baselines)
              ▼
         Python metrics (z-scores, C/P vs 30d, OI deltas, σ-moves — deterministic)
              ▼
         Analysis Agent ─► analysis_<date>.json / .md   (describes; never bullish/bearish)
              ▼
         Flagging Agent ─► shortlist_<date>.md / .json  (≤5 tickers or one line) + flag_log.csv
```

## Skills

| Step | Skill folder (`skills/<name>/SKILL.md`) | Role |
|---|---|---|
| 1 | `yahoo-finance-market-data-collector` | Gathers sourced, dated Yahoo data; never analyzes |
| 2 | `options-price-divergence-analyzer` | Describes where options activity and price disagree; never bullish/bearish |
| 3 | `unusual-options-research-shortlister` | ≤5-ticker research shortlist; never a trade |

The "what options volume actually tells you" principles are built into skills 2 and 3.
The file inside each folder must stay named `SKILL.md` (Claude Skills convention); the folder name is the skill's name.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env      # set ANTHROPIC_API_KEY (and optionally CLAUDE_MODEL) — .env is gitignored
python orchestrator.py run --watchlist watchlist.txt          # your live list; edit watchlist.txt to add/remove tickers
python orchestrator.py run --tickers AAPL NVDA --data-mode direct   # data step without the LLM
python orchestrator.py run --watchlist watchlist.txt --benchmarks benchmarks.example.json
python orchestrator.py run --watchlist watchlist.txt --oi-missing warn   # allow OI-unconfirmed flags (LOW)
python orchestrator.py review                                     # fill 10-session outcomes for past flags
python dashboard.py                                                # rebuild data/dashboard.md + run_log.csv / ticker_log.csv
python tests/test_offline.py                                      # offline test (fake Yahoo + fake Claude)
```

### Daily schedule

Run once a day, same time — **pre-market, ~08:30 ET, is recommended** (see "Things to know" below for why:
OI settlement timing and the 20-session volume baseline both depend on consistent daily timing). To automate it,
add a cron entry (adjust the path and consider `crontab -e` on macOS, which runs in your login timezone):

```cron
30 8 * * 1-5 cd /path/to/options-screener && /usr/bin/python3 orchestrator.py run --watchlist watchlist.txt >> data/cron.log 2>&1 && /usr/bin/python3 dashboard.py >> data/cron.log 2>&1
```

`watchlist.txt` (gitignored data aside, this file itself is meant to be edited and committed) is the list the
daily job reads — add or comment out tickers there without touching any code. `dashboard.py` is safe to run any
time; it only reads what `orchestrator.py run` already wrote to `data/<date>/` and rebuilds
`data/dashboard.md`, `data/run_log.csv`, and `data/ticker_log.csv` from scratch each time.

## Things to know before trusting the output

- **Run it once a day, same time** (pre-market, ~08:30 ET, is cleanest). Yahoo only publishes *current*
  open interest, so OI change needs yesterday's snapshot (available from day 2), and the options-volume
  "normal range" needs ~20 daily runs. Until then those fields read `UNAVAILABLE` / `INSUFFICIENT_HISTORY`,
  a volume-vs-prior-OI proxy is used, and flag confidence is capped at LOW.
- **No OI confirmation, no flag** (`--oi-missing block`, the default). With `--oi-missing warn`, flags
  with unavailable OI are kept but labelled "not evidence" and capped at LOW confidence. `CLOSED`/`MIXED`
  OI is never flagged.
- **Sector-wide check.** Each ticker is compared with its SPDR sector ETF (from Yahoo's `sector` field),
  SPY, and any peers you list in a benchmarks file (see `benchmarks.example.json`; you can also override
  the sector ETF). If the ETF or a peer shows unusual volume vs its *own* history, `sector_wide = YES`
  and confidence is capped at LOW. Benchmarks need their own daily history too.
- **OI lags one session.** Yahoo's OI reflects the prior session's OCC settlement, so today's volume is
  confirmed (opened vs closed) by *tomorrow's* OI.
- **Yahoo data is delayed** and occasionally wrong. `yfinance` is an unofficial client of Yahoo's endpoints;
  it can break or rate-limit. Failures become `UNAVAILABLE`, never estimates.
- **Guardrails in code, not just prompts:** numbers are written by tools and recomputed in Python (the
  agents' numbers are overwritten if they differ), banned words trigger a rejected-and-resubmit loop,
  the shortlist is hard-capped at 5 known tickers, and confidence is capped when the data can't support it.
- `flag_log.csv` + `review` is how you learn the real hit rate. Treat every flag as the start of research.
