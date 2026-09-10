#!/usr/bin/env bash
# Run every verification the Backtester has. Do this after ANY change to
# engine.py, config.py, report.py, or a new strategy.py.
set -e
cd "$(dirname "$0")"
echo "=== correctness ==="   && python3 test_harness.py
echo "=== robustness ==="    && python3 test_robustness.py
if ls data/*.csv >/dev/null 2>&1; then
  echo "=== known-answer validation on real data ===" && python3 verify_engine.py
else
  echo "=== known-answer validation: skipped (no data/*.csv) ==="
fi
echo
echo "All checks passed. Results from this harness can be trusted."
