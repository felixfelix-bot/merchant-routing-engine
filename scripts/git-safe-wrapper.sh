#!/usr/bin/env bash
# git-safe-wrapper.sh — Warn before operations that might lose uncommitted .env changes
#
# Usage:
#   git-safe-wrapper.sh checkout <args>
#   git-safe-wrapper.sh stash <args>
#   git-safe-wrapper.sh reset --hard <args>
#
# Add to .bashrc:
#   alias git-safe='~/merchant-routing-engine/scripts/git-safe-wrapper.sh'
#   alias checkout='git-safe checkout'
#   alias stash='git-safe stash'
set -euo pipefail

check_env_changes() {
    # Check for uncommitted changes to .env files
    ENV_CHANGES=$(git status --porcelain 2>/dev/null | grep -E "\.env" || true)
    if [ -n "$ENV_CHANGES" ]; then
        echo ""
        echo "⚠️  WARNING: Uncommitted .env changes detected!"
        echo "$ENV_CHANGES"
        echo ""
        echo "These changes will be LOST if you proceed."
        echo "Consider: 1) backup-env.sh  2) commit the template (.env.example)"
        echo ""
        read -p "Continue anyway? [y/N] " confirm
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            echo "Aborted."
            exit 1
        fi
    fi
}

# Check for .env files in working directory
check_env_present() {
    ENV_FILES=$(find . -name ".env" -type f 2>/dev/null | head -5 || true)
    if [ -n "$ENV_FILES" ]; then
        echo ""
        echo "ℹ️  .env files present in this repo:"
        echo "$ENV_FILES"
        echo ""
    fi
}

COMMAND="${1:-}"
shift || true

case "$COMMAND" in
    checkout|switch|reset|stash)
        check_env_changes
        git "$COMMAND" "$@"
        ;;
    *)
        git "$COMMAND" "$@"
        ;;
esac
