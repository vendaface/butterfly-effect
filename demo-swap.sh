#!/usr/bin/env bash
# demo-swap.sh — swap demo data in/out for screenshots
#
# Usage:
#   ./demo-swap.sh on    Back up real data → copy demo files in → restart server
#   ./demo-swap.sh off   Restore real data from backup → restart server

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DEMO_DIR="$APP_DIR/demo"
BACKUP_DIR="$DEMO_DIR/backup"

# Files that get swapped (relative to APP_DIR).
# .env is intentionally excluded — the API key isn't used in demo mode,
# and backing it up risks losing it if something goes wrong.
DATA_FILES=(
    config.yaml
    monarch_accounts_cache.json
    insights.json
    scenarios.json
    user_context.md
    payment_overrides.json
    payment_skips.json
)

cmd="${1:-}"

_swap_in() {
    # Idempotency guard — backup/config.yaml only exists when already in demo mode
    if [ -f "$BACKUP_DIR/config.yaml" ]; then
        echo "⚠  Already in demo mode. Run './demo-swap.sh off' first." >&2
        exit 1
    fi

    echo "Backing up real data to $BACKUP_DIR/ ..."
    mkdir -p "$BACKUP_DIR"
    for f in "${DATA_FILES[@]}"; do
        real="$APP_DIR/$f"
        if [ -e "$real" ]; then
            cp "$real" "$BACKUP_DIR/$f"
            echo "  ✓ backed up $f"
        else
            echo "  — $f does not exist (nothing to back up)"
        fi
    done

    echo ""
    echo "Copying demo data into place ..."
    for f in "${DATA_FILES[@]}"; do
        demo="$DEMO_DIR/$f"
        if [ -e "$demo" ]; then
            cp "$demo" "$APP_DIR/$f"
            echo "  ✓ $f"
        else
            echo "  — demo/$f not found (skipping)"
        fi
    done

    echo ""
    echo "Restarting server ..."
    "$APP_DIR/server.sh" restart

    echo ""
    echo "✓ Demo mode active at http://localhost:5002"
    echo "  Run './demo-swap.sh off' when you're done to restore your real data."
}

_swap_out() {
    if [ ! -f "$BACKUP_DIR/config.yaml" ]; then
        echo "⚠  No backup found at $BACKUP_DIR/config.yaml — is demo mode actually on?" >&2
        exit 1
    fi

    echo "Restoring real data from $BACKUP_DIR/ ..."
    for f in "${DATA_FILES[@]}"; do
        backup="$BACKUP_DIR/$f"
        if [ -f "$backup" ]; then
            cp "$backup" "$APP_DIR/$f"
            echo "  ✓ restored $f"
        else
            # File didn't exist before demo was turned on — remove the demo copy
            real="$APP_DIR/$f"
            if [ -e "$real" ]; then
                rm "$real"
                echo "  — removed $f (did not exist before demo)"
            fi
        fi
    done

    # Remove backup dir so the idempotency guard resets
    rm -rf "$BACKUP_DIR"

    echo ""
    echo "Restarting server ..."
    "$APP_DIR/server.sh" restart

    echo ""
    echo "✓ Real data restored at http://localhost:5002"
}

case "$cmd" in
    on)   _swap_in  ;;
    off)  _swap_out ;;
    *)
        echo "Usage: $0 on|off"
        echo ""
        echo "  on   Back up real data, swap in demo dataset, restart server"
        echo "  off  Restore real data from backup, restart server"
        exit 1
        ;;
esac
