#!/usr/bin/env bash
#
# deploy-nsites.sh — Deploy all nsite bundles with stable production keys.
#
# Usage:
#   ./deploy-nsites.sh                 # deploy all (display, participant, diagram)
#   ./deploy-nsites.sh display         # deploy only the display nsite
#   ./deploy-nsites.sh participant     # deploy only the participant nsite
#   ./deploy-nsites.sh diagram         # deploy only the diagram nsite
#
# Keys are read from gitignored files, NEVER hardcoded:
#   Display:     ~/.cvm-nsite-key (nsec)  →  demo/display-deploy/.nsite/deploy-key.hex (hex)
#   Participant: demo/participant/.nsite/deploy-key.hex (hex)
#   Diagram:     demo/diagram-nsite/.nsite/deploy-key.hex (hex)
#
# For dev/staging: run `nsyte init` in the target dir to generate a throwaway key.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$SCRIPT_DIR"

# --- Key resolution ----------------------------------------------------------

# Display key: prefer centralized nsec file, fall back to per-dir hex
get_display_key() {
  if [[ -f "$HOME/.cvm-nsite-key" ]]; then
    cat "$HOME/.cvm-nsite-key"
  elif [[ -f "$DEMO_DIR/display-deploy/.nsite/deploy-key.hex" ]]; then
    cat "$DEMO_DIR/display-deploy/.nsite/deploy-key.hex"
  else
    echo "ERROR: No display deploy key found." >&2
    echo "Create ~/.cvm-nsite-key (nsec) or display-deploy/.nsite/deploy-key.hex (hex)." >&2
    exit 1
  fi
}

get_hex_key() {
  local keyfile="$1"
  if [[ ! -f "$keyfile" ]]; then
    echo "ERROR: Key file not found: $keyfile" >&2
    exit 1
  fi
  cat "$keyfile"
}

# --- Deploy functions --------------------------------------------------------

deploy_display() {
  echo "=== Deploying display nsite ==="
  local key
  key="$(get_display_key)"
  cd "$DEMO_DIR/display-deploy"
  nsyte deploy . --sec "$key" --force
  echo "Display: https://npub1m4t7l3rtv99y4kv5dmx463lu3f5xxgu50spsmrkaq444v3kk5h3q35qh9g.nsite.lol"
}

deploy_participant() {
  echo "=== Deploying participant nsite ==="
  local key
  key="$(get_hex_key "$DEMO_DIR/participant/.nsite/deploy-key.hex")"
  cd "$DEMO_DIR/participant"
  nsyte deploy . --sec "$key" --force
  echo "Participant: https://npub13h0eushvdzdrygm545zr5zxr3qvk7zaqhrzyyepprflcjdu3u0yskxz53m.nsite.lol"
}

deploy_diagram() {
  echo "=== Deploying diagram nsite ==="
  local key
  key="$(get_hex_key "$DEMO_DIR/diagram-nsite/.nsite/deploy-key.hex")"
  cd "$DEMO_DIR/diagram-nsite"
  nsyte deploy . --sec "$key" --force
}

# --- Main --------------------------------------------------------------------

TARGET="${1:-all}"

case "$TARGET" in
  all)
    deploy_display
    deploy_participant
    deploy_diagram
    ;;
  display)    deploy_display ;;
  participant) deploy_participant ;;
  diagram)    deploy_diagram ;;
  *)
    echo "Usage: $0 [all|display|participant|diagram]" >&2
    exit 1
    ;;
esac

echo ""
echo "=== Deploy complete ==="
