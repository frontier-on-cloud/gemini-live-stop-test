#!/usr/bin/env bash
# Scenario G (barge-in while the model speaks with a NON_BLOCKING call pending), N=3,
# and the audio_C re-run with --save-audio (N=3). Both save the model's output audio to
# results/audio_out/. Appends one table per scenario to results/summary.md.
# Stops on a quota/billing error (stop_test.py exit status 3).
set -uo pipefail

cd "$(dirname "$0")"
N="${N:-3}"
MODALITY="${MODALITY:-AUDIO}"   # gemini-3.8-live rejected TEXT on 2026-09-29

if [ -z "${GEMINI_API_KEY:-}" ] && ! grep -qs '^GEMINI_API_KEY=.' .env; then
  echo "GEMINI_API_KEY is not set (put it in .env next to this script). Nothing was run." >&2
  exit 2
fi

STOPPED=0
run() {
  (( STOPPED )) && return
  echo
  echo "=== $1 ==="
  uv run stop_test.py --input audio --save-audio -n "$N" --modality "$MODALITY" --name "$@"
  status=$?
  if (( status == 3 )); then
    echo "quota/billing error in scenario $1: stopping"
    STOPPED=1
  elif (( status != 0 )); then
    echo "scenario $1 exited with status $status (continuing)"
  fi
}

# G2: book.wav, then bring.wav 0.5 s after the book_slot call; the stop clip starts
# 1.0 s after the first model audio chunk that follows the call, only if no tool
# response has been sent. Behavior unset (NON_BLOCKING default), service latency 7.0 s.
run audio_G2_default_speechstop1.0_lat7.0_honor \
  --followup-audio assets/audio/bring.wav --followup-after-tool-call 0.5 \
  --stop-after-model-speech 1.0 --latency 7.0 --honor-cancel --post-stop-window 15

# audio_C again (BLOCKING, stop clip 1.0 s after the tool call), to record the audio.
run audio_C_rerun_blocking_stop1.0_honor \
  --behavior BLOCKING --stop-after 1.0 --latency 4.0 --honor-cancel
