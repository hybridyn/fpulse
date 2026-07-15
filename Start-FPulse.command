#!/usr/bin/env bash
# Start-FPulse.command - double-clickable F-Pulse launcher for macOS
# (source / dev checkout). Double-click it in Finder to start F-Pulse and
# open it in your browser.
#
# Requires the `fpulse` CLI on PATH, i.e. you ran `pip install -e .` once
# in this folder. The packaged .pkg installer does not need this - it
# installs a real FPulse.app instead.
cd "$(dirname "$0")" || exit 1
if command -v fpulse >/dev/null 2>&1; then
  exec fpulse open
else
  echo ""
  echo "  The 'fpulse' command was not found on your PATH."
  echo "  From this folder run:   pip install -e ."
  echo "  then double-click Start-FPulse.command again."
  echo ""
  read -n 1 -s -r -p "  Press any key to close..."
  echo ""
  exit 1
fi
