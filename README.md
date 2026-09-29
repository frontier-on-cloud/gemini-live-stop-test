# live-stop-test

Discussion: [r/FrontierOnGCP](https://www.reddit.com/r/FrontierOnGCP/). License: MIT.

A small harness that measures what happens on the Gemini Live API
(`gemini-3.8-live`, Google AI Studio endpoint, `google-genai` Python SDK) when a
tool call is in flight and the user says "stop".

It started as a follow-up to a Reddit question: the assistant starts booking a
slot through a tool, the user says "actually, stop". Can the app still prevent
the booking?

## What one run does

One run is one Live session:

1. Sends `Book me the 3pm slot tomorrow, please.` with
   `send_realtime_input(text=...)`. If no tool call arrives within 15 s, it sends
   the same text with `send_client_content(..., turn_complete=True)` and records
   which method worked.
2. When `book_slot` arrives, it starts a job on a fake in-process
   `BookingService` that commits after `--latency` seconds (default 4.0). By
   default the tool response goes out as soon as the job commits.
3. `--stop-after` seconds after the tool call (default 1.0), it sends
   `Actually, stop. Don't book it.` with the same send method. With
   `--stop-after-request`, the timer starts at the booking request instead, so
   the stop can arrive before any tool call.
4. It records every server event with a timestamp in ms since session start
   (`time.monotonic()`): `interrupted`, `generation_complete`, `turn_complete`,
   `tool_call_cancellation` (with ids), model text or output transcription,
   audio chunk counts (bytes are discarded), `goAway`, errors, and the raw key
   names of anything the script does not handle.
5. With `--honor-cancel`, a `toolCallCancellation` that names the call id
   cancels the booking job. The harness records whether that happened before
   the commit or too late. Without the flag the job still commits and the tool
   response is still sent, so the harness can record whether the server objects.

A run ends when three things are true. The model has produced output after the
stop and then completed a turn. A `turn_complete` that only closes the
interrupted turn does not count. Every booking job has committed or been
cancelled. Any tool response sent after that turn has either triggered another
completed turn or been followed by `--quiet` (3 s) of silence. If
`--min-post-stop` is set, the run also lasts at least that long after the
stop. A run also ends 12 s after the stop, on a closed websocket, or at the
45 s hard timeout. The script sleeps 2 s between runs.

Output:

- `results/<scenario>.jsonl`: one JSON object per event. Each run ends with a
  `run_end` object holding the summary row plus what the model said before the
  stop.
- `results/summary.md`: one Markdown table per scenario.

## Run it

Requires [uv](https://docs.astral.sh/uv/). The project pins Python 3.13
(`.python-version`).

```sh
cp .env.example .env          # then set GEMINI_API_KEY=... in .env
uv run stop_test.py --help
uv run stop_test.py -n 1 --name smoke -v
./run_all.sh                  # matrix A-F, N=3 each; prints results/summary.md
MODALITY=AUDIO ./run_all.sh   # skip the TEXT probe (this model rejects TEXT)
```

`run_all.sh` moves earlier results to `results/archive-<timestamp>/` and does
not delete them. The key is read only from `GEMINI_API_KEY`, either in `.env`
here or in the environment. It is never printed or logged, and error strings
are redacted before they are written.

| scenario | behavior | stop after | latency | honor cancel | scheduling |
|---|---|---|---|---|---|
| A | unset (model default) | 1.0 s | 4.0 s | no | none |
| B | unset | 1.0 s | 4.0 s | yes | none |
| C | BLOCKING | 1.0 s | 4.0 s | yes | none |
| D | unset | 5.5 s (after commit) | 4.0 s | yes | none |
| E | unset | 1.0 s | 4.0 s | yes | SILENT |
| F | unset | 0.3 s after the booking request (before any tool call) | 4.0 s | yes | none |

Useful flags: `--behavior {unset,BLOCKING,NON_BLOCKING}`,
`--scheduling {none,INTERRUPT,WHEN_IDLE,SILENT}`,
`--scheduling-in {field,response,both}`, `--respond {immediate,never}`,
`--modality {auto,TEXT,AUDIO}`, `--send-method {auto,realtime,client_content}`.
`--stop-after-request S` starts the stop timer at the booking request instead
of at the tool call, and turns off the `send_client_content` fallback.
`--min-post-stop S` keeps listening at least S seconds after the stop. F uses
5 s so that a late `book_slot` call is still captured.

`--modality auto` tries `response_modalities=["TEXT"]` first. If the server
rejects it before any output, the run is repeated with `["AUDIO"]` and
`output_audio_transcription`, and the table header records the modality that
was used.

## Documented behaviour the harness relies on (checked 2026-09-29)

- [Live API reference](https://ai.google.dev/api/live):
  `BidiGenerateContentServerMessage` carries `setupComplete`, `serverContent`,
  `toolCall`, `toolCallCancellation`, `goAway`, `sessionResumptionUpdate`, and
  `usageMetadata`. `toolCallCancellation.ids[]` is described as a notification
  that "a previously issued ToolCallMessage with the specified ids should not
  have been executed and should be cancelled". Per that doc it occurs "only in
  cases where the clients interrupt server turns", and clients "may attempt to
  undo" side effects. `serverContent.interrupted` means a client message
  interrupted current model generation. `turnComplete` and
  `generationComplete` are separate booleans.
- [Live API tool use](https://ai.google.dev/gemini-api/docs/live-tools):
  function declarations accept `behavior: BLOCKING | NON_BLOCKING`, and
  responses to non-blocking calls accept a scheduling of `INTERRUPT`,
  `WHEN_IDLE`, or `SILENT`. The docs example puts `"scheduling"` inside the
  `response` dict. The client must send tool responses itself
  (`send_tool_response`).
- [gemini-3.8-live model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live):
  "Async execution (behavior: NON_BLOCKING) is now the default function calling
  mode." BLOCKING is still available.

## SDK introspection (google-genai 2.25.0, 2026-09-29)

`uv run introspect.py` prints the SDK surface below. It makes no network calls
and does not need a key. The harness uses these names:

- `LiveServerMessage`: `tool_call`, `tool_call_cancellation` (`.ids`),
  `server_content`, `go_away` (`.time_left`), `usage_metadata`,
  `setup_complete`, `session_resumption_update`. This version also has
  `voice_activity` and `voice_activity_detection_signal`, which are not in the
  doc list above.
- `LiveServerContent`: `interrupted`, `turn_complete`, `generation_complete`,
  `output_transcription` (`.text`), and `model_turn`. Fields not in the docs
  summary above: `turn_complete_reason`, `waiting_for_input`,
  `interaction_status` (`IN_PROGRESS`, `REQUIRES_ACTION`, `IDLE`),
  `interim_input_transcription`. The harness logs them when they are set.
- `FunctionDeclaration.behavior`: `types.Behavior` has `UNSPECIFIED`,
  `BLOCKING`, `NON_BLOCKING`. "unset" in this harness means the field is not
  sent.
- `AsyncSession.send_realtime_input(*, media, audio, audio_stream_end, video,
  text, activity_start, activity_end)`,
  `send_client_content(*, turns, turn_complete=True)`,
  `send_tool_response(*, function_responses)`.

Where the SDK differs from the docs, or from what you might assume:

1. `FunctionResponse.scheduling` is a typed top-level field
   (`types.FunctionResponseScheduling`: `SCHEDULING_UNSPECIFIED`, `SILENT`,
   `WHEN_IDLE`, `INTERRUPT`). The SDK docstring gives `WHEN_IDLE` as the
   default. The docs example puts `scheduling` inside the `response` dict
   instead, so the harness sends both by default (`--scheduling-in both`).
   `FunctionResponse` also has `will_continue`.
2. `session.receive()` returns after each completed turn (`turn_complete`, or
   `interaction_status == IDLE` when set). The harness loops around it to keep
   listening for the whole session.
3. `connect()` waits for the server's setup reply before it yields the
   session, so "setup complete" is implicit.
4. On the AI Studio path the SDK maps only the top-level server keys it knows.
   Unknown keys are dropped silently. A message made only of unknown keys, or
   an in-band `{"error": ...}` payload without a top-level `code`, reaches
   `APIError.raise_error(None, ...)` in `live.py`, and in this version that
   raises a `TypeError` from `receive()`. The harness wraps the websocket to log
   raw key names, logs that error, and keeps receiving.

## Findings (2026-09-29)

Across the 15 sessions of scenarios A-E, the server never sent
`toolCallCancellation`. In the nine default-behaviour runs (A, B, E), the
model's first turn contained only the `book_slot` call and no speech. In the
same nine runs, the model told the user the booking "was already made"
0.5-0.9 s after the stop. At that moment the fake service was still about 2 s
from committing, and no tool response had been sent. In all three BLOCKING
runs (C), `interrupted` arrived 16-18 ms after the stop, and 0.6-0.8 s later
the model issued a second `book_slot` call with a new id and the same slot.
Both fake bookings committed, and the model then said the slot was already
booked. Because no cancellation arrived, `--honor-cancel` never had anything
to act on, and B behaved like A.

Details, all in `results/summary.md` and the JSONL timelines:

- `response_modalities=["TEXT"]` was rejected. The websocket closed with code
  1007: "The requested combination of response modalities (TEXT) is not
  supported by the model." Every run used AUDIO with output transcription.
- E (`SILENT`): the model did not speak after the tool response.
- D, first pass: these runs ended on the `turn_complete` that closed the
  interrupted turn, before the model answered the stop. That was a bug in the
  harness stop condition. It is fixed, and the D section in `summary.md` is
  marked.
- D re-run (3 sessions, fixed stop condition): the model began saying "I am
  booking the 3pm slot..." 0.5-0.9 s after the tool response. The stop at
  5.5 s interrupted that speech, and `interrupted` arrived 15-16 ms after the
  stop. There was no `toolCallCancellation`. 0.5-0.7 s after the stop, the
  model said the booking was already made, which matched the fake service.
- F (3 sessions, stop sent 0.3 s after the booking request, before any tool
  call): there was no `interrupted` and no `toolCallCancellation` in any run.
  In run 1 no `book_slot` call arrived, and the model said "I haven't made
  that booking for you." In runs 2 and 3, `book_slot` arrived 1.05 s and
  1.12 s after the stop. The fake booking committed, and after the tool
  response the model said the booking was already made.

## What this does not establish

- The booking service is a fake in-process `asyncio` job, not a real backend.
  Cancelling it is an `asyncio.Task.cancel()`.
- Input is typed text (`send_realtime_input(text=...)` or
  `send_client_content`), not speech. With real audio, VAD and barge-in timing
  change when the server sees the "stop".
- It uses the Google AI Studio endpoint with an API key, not Vertex AI. It
  does not cover other models, and it does not cover session resumption.
- N=3 per scenario on one day. That shows what can happen, not how often.
  Model wording varies between runs. D was run twice: the first pass was
  affected by the stop-condition bug, and the second is the D re-run.
- "What the model said after stop" counts everything received after the stop
  was sent, including audio or text generated just before it that arrived
  late.

## Figure

`results/figures/stop-timeline-A1-C1.png` shows run 1 of scenario A (default) and run 1 of scenario C (BLOCKING) on one time axis. Regenerate it with `uv run --with matplotlib python make_figure.py`.

## Files

- `stop_test.py`: the harness (argparse CLI, async, one session per run).
- `run_all.sh`: runs scenarios A-F with N=3 and prints the summary.
- `introspect.py`: prints the SDK version and the fields and signatures used.
- `.env.example`: `GEMINI_API_KEY=` placeholder. `.env` is git-ignored.
- `results/`: JSONL timelines and `summary.md`.
