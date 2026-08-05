#!/usr/bin/env bash
# RP-2 deployment: restart zai-proxy to activate real cost extraction.
#
# Usage:  bash scripts/deploy_rp2.sh
# Revert: bash scripts/deploy_rp2.sh --revert
#
# Backups:
#   ~/.hermes/bot/zai_proxy.py.bak-rp2-<timestamp>  (created during patching)
set -euo pipefail

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

PROXY="$HOME/.hermes/bot/zai_proxy.py"
LATEST_BAK=$(ls -t "$PROXY".bak-rp2-* 2>/dev/null | head -1)

if [[ "${1:-}" == "--revert" ]]; then
    if [[ -z "$LATEST_BAK" ]]; then
        echo "ERROR: no RP-2 backup found to revert from"
        exit 1
    fi
    echo "Reverting from: $LATEST_BAK"
    cp "$LATEST_BAK" "$PROXY"
    systemctl --user restart zai-proxy
    echo "Reverted + restarted."
    exit 0
fi

echo "=== Pre-flight checks ==="
python3 -m py_compile "$PROXY" && echo "  syntax: OK"

echo "=== Restarting zai-proxy ==="
systemctl --user restart zai-proxy
sleep 3

if systemctl --user is-active --quiet zai-proxy; then
    echo "  service: ACTIVE"
    NEW_PID=$(pgrep -f "zai_proxy.py" | head -1)
    echo "  new PID: $NEW_PID"
    echo "=== RP-2 deployed successfully ==="
else
    echo "  service: FAILED — auto-reverting"
    if [[ -n "$LATEST_BAK" ]]; then
        cp "$LATEST_BAK" "$PROXY"
        systemctl --user restart zai-proxy
        echo "  reverted to: $LATEST_BAK"
    fi
    exit 1
fi
