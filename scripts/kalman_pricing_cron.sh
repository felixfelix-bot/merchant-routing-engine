#!/bin/bash
# kalman_pricing_cron.sh — Refresh Kalman pricing export for Routstr integration.
#
# Runs export_kalman_pricing.py every 5 minutes to produce
# ~/.routstrd/kalman_pricing.json, which the Routstr daemon's
# KalmanPricingBridge reads to overlay Kalman-adjusted sats_pricing
# on the model cache.
#
# Usage: Add to crontab:
#   */5 * * * * /home/c03rad0r/merchant-routing-engine/scripts/kalman_pricing_cron.sh
#
# Or run directly:
#   ./kalman_pricing_cron.sh
#
# Env: KALMAN_PYTHON  interpreter to use (default /usr/bin/python3, which has
#                     numpy; an interactive PATH python3 often does not)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORT_SCRIPT="$SCRIPT_DIR/export_kalman_pricing.py"
OUTPUT_FILE="${HOME}/.routstrd/kalman_pricing.json"
LOG_FILE="${HOME}/.routstrd/logs/kalman_pricing_cron.log"

# The export imports the merchant routing engine's pricing_engine, which needs
# numpy. `python3` resolved from an interactive PATH (~/.local/bin) lacks it
# (ModuleNotFoundError: numpy), so pin the system interpreter unless overridden.
PYTHON_BIN="${KALMAN_PYTHON:-/usr/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: no usable python3 interpreter" >> "$LOG_FILE"
    exit 1
fi

mkdir -p "$(dirname "$OUTPUT_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "[$(timestamp)] Running Kalman pricing export..." >> "$LOG_FILE"

if [ ! -f "$EXPORT_SCRIPT" ]; then
    echo "[$(timestamp)] ERROR: Export script not found at $EXPORT_SCRIPT" >> "$LOG_FILE"
    exit 1
fi

# `set -e` makes a bare "$?" check unreachable on failure — use an if so the
# error branch actually runs and the log records why.
if "$PYTHON_BIN" "$EXPORT_SCRIPT" \
        --out "$OUTPUT_FILE" \
        --btc-price "${KALMAN_BTC_PRICE_USD:-95000}" \
        >> "$LOG_FILE" 2>&1; then
    echo "[$(timestamp)] SUCCESS: Pricing exported to $OUTPUT_FILE" >> "$LOG_FILE"
else
    echo "[$(timestamp)] ERROR: Export failed" >> "$LOG_FILE"
    exit 1
fi