#!/usr/bin/env bash
# Add a crontab line to run the Bitaxe health check every 5 minutes.
# Prints the line and asks before touching the crontab.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
LOGDIR="$HOME/.local/state/bitaxe-health"
LINE="*/5 * * * * $PYTHON $DIR/bitaxe_health.py --config $DIR/config.toml >> $LOGDIR/cron.log 2>&1"

echo "This crontab line will be added:"
echo
echo "  $LINE"
echo

if crontab -l 2>/dev/null | grep -Fq "$DIR/bitaxe_health.py"; then
  echo "A line for bitaxe_health.py already exists in your crontab. Nothing to do."
  echo "Edit it manually with: crontab -e"
  exit 0
fi

read -r -p "Add it now? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
  mkdir -p "$LOGDIR"
  ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
  echo "Added. Check with: crontab -l"
else
  echo "Skipped. To add manually, run 'crontab -e' and paste the line above."
fi
