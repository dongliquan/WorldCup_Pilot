#!/usr/bin/env bash
# Run a command and, when it fails, copy its error lines into a GitHub annotation so the cause shows on the run's
# summary page without opening the step log.   usage: ci-step.sh "<title>" <command> [args...]
title="$1"; shift
log=".ci-step.log"
set -o pipefail
"$@" 2>&1 | tee "$log"
code=${PIPESTATUS[0]}
if [ "$code" -ne 0 ]; then
  msg=$(grep -iE 'error|ERR!|fail|cannot|not found|denied' "$log" | tail -n 12 | tr '\n' '|' | cut -c1-2500)
  echo "::error title=${title} failed::${msg}"
fi
exit "$code"
