#!/bin/bash
# Daily screener run: Data -> Analysis -> Flagging, rebuild the dashboard CSVs,
# then sync just the small summary files (data/run_log.csv, ticker_log.csv,
# flag_log.csv, dashboard.md) into the GitHub-connected clone and push, so the
# deployed Streamlit Cloud app (which reads only those files) picks up today's
# results within a minute or two. Raw per-day JSON stays local only.
#
# Intended to run from cron (see README "Daily schedule"). Logs to data/cron.log.
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
LOG="data/cron.log"
mkdir -p data

# Where the GitHub-connected clone of this project lives (adjust if you move it).
CLONE_DIR="/Users/luciazhang/github-path-luciaz0/test-for-run/stock-performance-analysis"

{
  echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) starting daily run ====="

  "$PY" orchestrator.py run --watchlist watchlist.txt
  "$PY" dashboard.py

  if [ -d "$CLONE_DIR/.git" ] || git -C "$CLONE_DIR" rev-parse 2>/dev/null; then
    mkdir -p "$CLONE_DIR/data"
    cp data/run_log.csv data/ticker_log.csv data/dashboard.md "$CLONE_DIR/data/" 2>/dev/null || true
    [ -f data/flag_log.csv ] && cp data/flag_log.csv "$CLONE_DIR/data/" || true

    ( cd "$CLONE_DIR" && \
      git add data/run_log.csv data/ticker_log.csv data/flag_log.csv data/dashboard.md 2>/dev/null; \
      if ! git diff --cached --quiet; then
        git commit -m "Daily screener run $(date -u +%Y-%m-%d)"
        git push origin projects
        echo "Pushed daily update to GitHub."
      else
        echo "No changes to commit (dashboard identical to last run)."
      fi )
  else
    echo "WARNING: GitHub clone not found at $CLONE_DIR — skipped sync/push."
  fi

  echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) done ====="
} >> "$LOG" 2>&1
