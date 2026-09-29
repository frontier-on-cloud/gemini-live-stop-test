#!/usr/bin/env bash
# Run the A-F matrix (N=3 sessions each) and print results/summary.md.
# Previous results are moved to results/archive-<timestamp>/ (never deleted).
# MODALITY=auto tries TEXT first; gemini-3.8-live rejected TEXT on 2026-09-29,
# so `MODALITY=AUDIO ./run_all.sh` skips that probe.
set -uo pipefail

cd "$(dirname "$0")"
N="${N:-3}"
MODALITY="${MODALITY:-auto}"
RESULTS="results"
mkdir -p "$RESULTS"

shopt -s nullglob
old=("$RESULTS"/*.jsonl)
if [ -f "$RESULTS/summary.md" ]; then old+=("$RESULTS/summary.md"); fi
if (( ${#old[@]} )); then
  archive="$RESULTS/archive-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$archive"
  mv "${old[@]}" "$archive"/
  echo "previous results moved to $archive"
fi

{
  echo "# live-stop-test summary"
  echo
  echo "Run started $(date '+%Y-%m-%d %H:%M %Z'). google-genai $(uv run python -c 'from importlib.metadata import version; print(version("google-genai"))'). N=$N per scenario, modality=$MODALITY."
  echo
} > "$RESULTS/summary.md"

STOPPED=0
run() {
  (( STOPPED )) && return
  echo
  echo "=== $1 ==="
  uv run stop_test.py -n "$N" --modality "$MODALITY" --name "$@"
  status=$?
  if (( status == 3 )); then
    echo "quota/billing error in scenario $1: stopping the matrix"
    STOPPED=1
  elif (( status != 0 )); then
    echo "scenario $1 exited with status $status (continuing)"
  fi
}

run A_default_stop1.0_nohonor      --stop-after 1.0 --latency 4.0
run B_default_stop1.0_honor        --stop-after 1.0 --latency 4.0 --honor-cancel
run C_blocking_stop1.0_honor       --behavior BLOCKING --stop-after 1.0 --latency 4.0 --honor-cancel
run D_default_stop5.5_honor        --stop-after 5.5 --latency 4.0 --honor-cancel
run E_default_silent_stop1.0_honor --scheduling SILENT --stop-after 1.0 --latency 4.0 --honor-cancel
run F_default_stopreq0.3_honor     --stop-after-request 0.3 --min-post-stop 5 --latency 4.0 --honor-cancel

echo
echo "================ results/summary.md ================"
cat "$RESULTS/summary.md"
