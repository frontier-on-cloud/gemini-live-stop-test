#!/usr/bin/env bash
# Run the audio-input matrix (N=3 sessions each): A, C, D and F with --input audio.
# The utterances are streamed from assets/audio/{book,stop}.wav as 16 kHz PCM.
# Appends an "Audio input, <date>" section to results/summary.md; the text-input
# results are left alone. Earlier results/audio_*.jsonl files are moved to
# results/archive-<timestamp>/ (never deleted). Stops on a quota/billing error.
set -uo pipefail

cd "$(dirname "$0")"
N="${N:-3}"
MODALITY="${MODALITY:-AUDIO}"   # gemini-3.8-live rejected TEXT on 2026-09-29
RESULTS="results"
mkdir -p "$RESULTS"

# Fail before touching results/ if there is no key (the value is never printed).
if [ -z "${GEMINI_API_KEY:-}" ] && ! grep -qs '^GEMINI_API_KEY=.' .env; then
  echo "GEMINI_API_KEY is not set (put it in .env next to this script). Nothing was run." >&2
  exit 2
fi

shopt -s nullglob
old=("$RESULTS"/audio_*.jsonl)
if (( ${#old[@]} )); then
  archive="$RESULTS/archive-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$archive"
  mv "${old[@]}" "$archive"/
  echo "previous audio results moved to $archive"
fi

clips=$(uv run python -c '
import wave
out = []
for n in ("book", "stop"):
    with wave.open(f"assets/audio/{n}.wav", "rb") as w:
        out.append(f"{n}.wav {w.getnframes() / w.getframerate():.3f} s")
print(", ".join(out))')

{
  echo "## Audio input, $(date +%Y-%m-%d)"
  echo
  echo "Run started $(date '+%Y-%m-%d %H:%M %Z'). google-genai $(uv run python -c 'from importlib.metadata import version; print(version("google-genai"))'). N=$N per scenario, modality=$MODALITY, input=audio ($clips; 16 kHz 16-bit mono PCM, 100 ms chunks paced in real time, silence between utterances, automatic VAD at server default). \`stop_sent_at_ms\` is the first chunk of the stop clip and \`stop_audio_end_ms\` the last. \`interrupted_seen\` offsets are relative to \`stop_sent_at_ms\`."
  echo
} >> "$RESULTS/summary.md"
start_line=$(grep -n "^## Audio input" "$RESULTS/summary.md" | tail -1 | cut -d: -f1)

STOPPED=0
run() {
  (( STOPPED )) && return
  echo
  echo "=== $1 ==="
  uv run stop_test.py --input audio -n "$N" --modality "$MODALITY" --name "$@"
  status=$?
  if (( status == 3 )); then
    echo "quota/billing error in scenario $1: stopping the matrix"
    STOPPED=1
  elif (( status != 0 )); then
    echo "scenario $1 exited with status $status (continuing)"
  fi
}

run audio_A_default_stop1.0_honor   --stop-after 1.0 --latency 4.0 --honor-cancel
run audio_C_blocking_stop1.0_honor  --behavior BLOCKING --stop-after 1.0 --latency 4.0 --honor-cancel
run audio_D_default_stop5.5_honor   --stop-after 5.5 --latency 4.0 --honor-cancel
run audio_F_default_stopend0.3_honor --stop-after-request 0.3 --min-post-stop 5 --latency 4.0 --honor-cancel

echo
echo "================ results/summary.md (audio section) ================"
tail -n "+$start_line" "$RESULTS/summary.md"
