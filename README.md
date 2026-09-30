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

### Audio input

`--input audio` sends the two utterances as speech instead of text. The clips
in `assets/audio/` were made locally with macOS `say` (voice Samantha), written
directly as 16 kHz, 16-bit little-endian mono PCM WAV:

```sh
say -v Samantha --file-format=WAVE --data-format=LEI16@16000 \
    -o assets/audio/book.wav "Book me the 3pm slot tomorrow, please."
say -v Samantha --file-format=WAVE --data-format=LEI16@16000 \
    -o assets/audio/stop.wav "Actually, stop. Don't book it."
# scenario G (2026-09-30)
say -v Samantha --file-format=WAVE --data-format=LEI16@16000 \
    -o assets/audio/book_bring.wav "Book me the 3pm slot tomorrow, and while you do that, tell me what I should bring to the appointment."
say -v Samantha --file-format=WAVE --data-format=LEI16@16000 \
    -o assets/audio/bring.wav "While you do that, what should I bring to the appointment?"
```

| clip | text | duration | samples | 100 ms chunks | speech (\|x\| > 300) |
|---|---|---|---|---|---|
| `book.wav` | Book me the 3pm slot tomorrow, please. | 2.686 s | 42,977 | 27 | 0.006-2.643 s |
| `stop.wav` | Actually, stop. Don't book it. | 2.226 s | 35,613 | 23 | 0.006-2.189 s |
| `book_bring.wav` (G1) | Book me the 3pm slot tomorrow, and while you do that, tell me what I should bring to the appointment. | 5.647 s | 90,346 | 57 | 0.006-5.611 s |
| `bring.wav` (G2) | While you do that, what should I bring to the appointment? | 2.870 s | 45,921 | 29 | 0.005-2.831 s |

(Checked with Python `wave` and `afinfo`: 16000 Hz, 1 channel, Int16.)

How they are sent:

- The harness reads the PCM frames with Python `wave` (and refuses anything
  that is not 16 kHz, 16-bit, mono) and streams them through a simulated open
  microphone. Each send is one
  `send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))`
  carrying 100 ms (3,200 bytes; the last chunk of a clip is shorter). Sends
  are paced in real time against an absolute schedule, so a clip takes about
  as long to send as to play.
- Between and after the utterances, the mic keeps sending 100 ms chunks of
  digital silence, as a live microphone would, so the server's VAD can see the
  end of speech. No `audio_stream_end`, `activity_start`, or `activity_end` is
  sent.
- If the stop timer fires while the mic is sending silence, the first stop
  chunk goes out at once instead of waiting for the next 100 ms tick. The audio
  the server has received can therefore be up to about 100 ms ahead of wall
  clock time per utterance.
- `realtime_input_config` and `explicit_vad_signal` are not set, so automatic
  activity detection runs at the server default (enabled, default
  sensitivities, default barge-in handling).
- `input_audio_transcription` is turned on in audio mode so the log shows what
  the server heard (`input_transcript` events). The text runs did not turn it
  on. Voice activity messages are logged as they arrive: `voiceActivity`
  (`ACTIVITY_START` / `ACTIVITY_END`, `audioOffset`) as `voice_activity`,
  `voiceActivityDetectionSignal` (`VAD_SIGNAL_TYPE_SOS` / `EOS`) as
  `vad_signal`, both also raw as `raw_vad`, and `interimInputTranscription`
  as `interim_input_transcript`.
- There is no `send_client_content` fallback. If no tool call arrives within
  15 s of the end of the booking clip, the run ends as `no_tool_call`.

What the timestamps mean (ms since session start, taken when the send call
returns):

- `user_audio_start` / `user_audio_end` events: the first and last chunk of
  each clip.
- `stop_sent_at_ms`: the first chunk of the stop clip. `--stop-after` counts
  from the tool call to this point, and the `interrupted_seen` offsets are
  relative to it. `stop_audio_end_ms`: the last chunk of the stop clip, about
  2.2 s later.
- `--stop-after-request` counts from the last chunk of the booking clip.
- The run-end rules above, `--min-post-stop`, and the 12 s post-stop window
  count from `stop_audio_end_ms`, because the model cannot answer the stop
  before it has heard all of it. `model_after_stop` still counts from
  `stop_sent_at_ms`. The JSONL `run_end` summary also splits it into
  `model_during_stop_clip` and `model_after_stop_audio_end`, and lists the VAD
  events and the input transcript.
- None of these times is when the server detected speech. The server acts on
  speech only after its VAD has detected the start (and, for a reply, the
  end) of it, so there is a gap after the first chunk and after the last chunk
  that the harness does not measure directly. `voice_activity` events, when
  the server sends them, are the closest marker.

`run_audio.sh` runs this matrix with `--input audio`, N=3 each, and appends
an "Audio input" section to `results/summary.md` (text results are kept):

| scenario | behavior | stop clip starts | latency | honor cancel |
|---|---|---|---|---|
| audio_A | unset | 1.0 s after the tool call | 4.0 s | yes |
| audio_C | BLOCKING | 1.0 s after the tool call | 4.0 s | yes |
| audio_D | unset | 5.5 s after the tool call (after the commit) | 4.0 s | yes |
| audio_F | unset | 0.3 s after the end of the booking clip (`--min-post-stop 5`, as F) | 4.0 s | yes |

### Scenario G: barge-in while the model speaks, call still pending

In every default-behaviour run above, the model's turn held only the
`book_slot` call and the model stayed silent until the tool response, so the
stop never landed on model speech while the call was pending. G sets that up:

- `--input audio`, behavior unset (NON_BLOCKING is the model default),
  `--honor-cancel`, `--latency 7.0` so the call is still pending while the
  model speaks, `--post-stop-window 15`.
- `--stop-after-model-speech S` starts the stop clip S seconds after the
  first model audio chunk that arrives after the `book_slot` call, and only if
  no tool response has been sent yet. Otherwise the stop is skipped (a
  `stop_skipped` event, with the reason in the `stop_sent_at_ms` column), and
  the run ends once every job is done and the model has been quiet for
  `--quiet` s.
- G1 replaced the request with `--book-audio assets/audio/book_bring.wav`.
  In its smoke session the model said nothing before the tool response, so
  G1 was dropped.
- G2 keeps `book.wav`, and `--followup-audio assets/audio/bring.wav
  --followup-after-tool-call 0.5` streams a second question 0.5 s after the
  call arrives. The model answers it while the call is pending, and the stop
  lands on that answer.
- G tables add `model_speech_start_ms` (the first model audio chunk after the
  call, and its offset from the call) and `cancellation_ids_match` (whether
  the `toolCallCancellation` ids name the call that was pending when the stop
  clip started). The JSONL `run_end` summary also lists the tool calls, the
  pending ids, cancellations, service outcomes, tool responses, the
  `interrupted` / `generation_complete` / `turn_complete` times, the model
  turns and input transcripts with times, and what the model said after the
  tool response.

`run_g.sh` runs G2 and an audio_C re-run, N=3 each, with `--save-audio`.

### Saving audio (`--save-audio`)

Per run, in `results/audio_out/`:

- `<scenario>_run<N>_model.wav`: every model audio chunk received,
  concatenated in arrival order, mono 16-bit. The sample rate comes from the
  chunk mime type (`audio/pcm;rate=24000` in every run so far); 24 kHz is the
  fallback if the mime type has no rate.
- `<scenario>_run<N>_audio.json`: the sidecar. Per chunk: `t_ms` (arrival,
  ms since session start), `turn`, `bytes`, `wav_offset_ms`, `duration_ms`.
  The server sends audio faster than real time (in G2, about 5 s of audio
  within about 1.2 s), so place each chunk at its `t_ms` or later, not at
  `wav_offset_ms`. The WAV keeps everything received, including audio that a
  client playing in real time would still have had queued when
  `interrupted` arrived. The sidecar also lists the user clips with
  `sent_start_ms` / `sent_end_ms` (first and last chunk sent), and the tool
  calls, tool responses, service outcomes, cancellations, `interrupted`,
  `generation_complete` and `turn_complete` times, voice activity, and the
  model and input transcripts with times.
- `<scenario>_run<N>_user_<label>.wav`: copies of the clips that were sent
  (`book_request`, `followup`, `stop`), 16 kHz.

Output:

- `results/<scenario>.jsonl`: one JSON object per event. Each run ends with a
  `run_end` object holding the summary row plus what the model said before the
  stop.
- `results/summary.md`: one Markdown table per scenario. The tables have an
  `input` column (`text` or `audio`); audio tables also have
  `stop_audio_end_ms`.

## Run it

Requires [uv](https://docs.astral.sh/uv/). The project pins Python 3.13
(`.python-version`).

```sh
cp .env.example .env          # then set GEMINI_API_KEY=... in .env
uv run stop_test.py --help
uv run stop_test.py -n 1 --name smoke -v
./run_all.sh                  # matrix A-F, N=3 each; prints results/summary.md
MODALITY=AUDIO ./run_all.sh   # skip the TEXT probe (this model rejects TEXT)
./run_audio.sh                # audio input: A, C, D, F with N=3; appends to summary.md
./run_g.sh                    # G2 and an audio_C re-run, N=3 each, with --save-audio
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
`--modality {auto,TEXT,AUDIO}`, `--send-method {auto,realtime,client_content}`,
`--input {text,audio}` (with `--book-audio` and `--stop-audio` to use other
16 kHz mono WAV files), `--stop-after-model-speech S`,
`--followup-audio WAV` with `--followup-after-tool-call S`, `--save-audio`.
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
- Audio input: `send_realtime_input` accepts exactly one argument per call
  (more raises `ValueError`). `audio` takes a `types.Blob` with fields `data`
  (bytes), `mime_type`, and `display_name`. The SDK rejects a mime type that
  does not start with `audio/`, base64-encodes `data`, and sends
  `{"realtime_input": {"audio": {...}}}`. VAD settings live in
  `LiveConnectConfig.realtime_input_config` (`automatic_activity_detection`
  with `disabled`, start/end sensitivities, `prefix_padding_ms`,
  `silence_duration_ms`; `activity_handling`; `turn_coverage`) and
  `explicit_vad_signal`. The harness sets none of them. Server-side,
  `VoiceActivity` has `voice_activity_type` (`ACTIVITY_START`,
  `ACTIVITY_END`) and `audio_offset`, and `VoiceActivityDetectionSignal` has
  `vad_signal_type` (`VAD_SIGNAL_TYPE_SOS`, `VAD_SIGNAL_TYPE_EOS`).

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

In the 21 text-input sessions (A-F and the D re-run, N=3 each), the server
never sent `toolCallCancellation`. In the nine default-behaviour runs (A, B, E), the
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

## Findings, audio input (2026-09-29)

In the 12 audio-input sessions (audio_A, audio_C, audio_D, audio_F, N=3
each), the server never sent `toolCallCancellation` (0 of 12), so
`--honor-cancel` had nothing to act on. Counting both inputs, the results
hold 33 sessions (21 text, 12 audio). Leaving out the first D pass, which
the stop-condition bug cut short, that is 30 (18 text + 12 audio). None of
the 33 had a `toolCallCancellation`. The server sent `voiceActivity`
messages in every audio run. It transcribed the clips as "Book me the 3:00
p.m. slot tomorrow, please." and "Actually, stop. Don't book it."
`ACTIVITY_START` for the stop came 139-154 ms after the first chunk of the
stop clip (A, C, D). `ACTIVITY_END` came 1188-1279 ms after its last chunk
(all 12 runs). `interrupted` arrived in all C and D runs, 149-152 ms (C) and
139-144 ms (D) after the stop clip started, in the same millisecond as
`ACTIVITY_START` or 1 ms after it. It did not arrive in A or F.

- audio_A: the model's first turn contained only the `book_slot` call. The
  server's `ACTIVITY_END` for the stop came 473-482 ms after the fake
  booking committed, and 473-477 ms after the tool response was sent. The
  model then said the slot "was already booked", "has already been booked",
  or that the booking "was already made". It started speaking 1056-1130 ms
  after the commit and 1050-1130 ms after the tool response. In text A and
  B, the same statement came before the commit.
- audio_C (BLOCKING): run 2 double-booked. A second `book_slot` call with a
  new id and the same slot arrived 183 ms after `ACTIVITY_END`. That was
  3658 ms after the stop clip started and 1456 ms after its last chunk. Both
  bookings committed (8177 and 12836 ms), and 728 ms after the second commit
  the model said the booking was already made. Runs 1 and 3 committed once
  (8083 and 8123 ms), with no second call. In run 1 the model said "I have
  not booked the slot. It has been canceled as requested." In run 3 it said
  "I've stopped the process, and the booking booking was not made." Those
  statements started 632 and 793 ms after the commit, and 625 and 792 ms
  after the tool response that reported `status: booked`. That tool
  response had been sent 472 and 475 ms before the server's `ACTIVITY_END`
  for the stop.
- audio_D: 519-659 ms after the tool response, the model said "I'm booking
  the 3 p.m. slot..." Its `generation_complete` came 104-310 ms before the
  stop clip started, and `interrupted` still arrived 139-144 ms after the
  stop clip started. 584-786 ms after `ACTIVITY_END`, the model said the
  booking was already made, which matched the fake service.
- audio_F (stop clip starts 0.3 s after the last chunk of the booking clip):
  the server sent one `ACTIVITY_START` and one `ACTIVITY_END` covering both
  clips. It transcribed them as one turn: "Book me the 3:00 p.m. slot
  tomorrow, please. Actually, stop, don't book it." In 3 of 3 runs,
  `book_slot` arrived 3465-3475 ms after the stop clip started. That was
  after the clip had ended: 1263-1273 ms after its last chunk, and 0-1 ms
  after `ACTIVITY_END`. The fake booking committed 4.0 s later. 764-874 ms
  after the tool response, the model said the booking was already made. In
  text F, 2 of 3 runs got a `book_slot` call, 1.05 s and 1.12 s after the
  stop.

`results/summary.md` ends with a text vs audio table for A, C, D and F.

## Findings, barge-in while the model speaks (2026-09-30)

Eight audio sessions: G1 smoke (1), G2 smoke (1), G2 (N=3), and an audio_C
re-run with `--save-audio` (N=3). With them, the results hold 41 sessions
(21 text, 20 audio), and none of the 41 had a `toolCallCancellation`.

- G1 (smoke): with the request "Book me the 3pm slot tomorrow, and while you
  do that, tell me what I should bring to the appointment.", the model's turn
  held only the `book_slot` call. It said nothing for 7.6 s, until 626 ms
  after the tool response, and then confirmed the booking and answered the
  question in one turn.
- G2 (smoke plus N=3): after the follow-up question, the model spoke while
  the call was pending, 4856-4918 ms after the call, for example "I am
  booking the 3 PM slot for you now. Please bring your ID and any necessary
  paperwork to the appointment." When `interrupted` arrived, the harness had
  already received 4.7-5.4 s of audio for that turn, over 1.1-1.2 s of wall
  time. In all four sessions, `interrupted` arrived in the same millisecond
  as `ACTIVITY_START` for the stop, 144-238 ms after the stop clip started.
  No `toolCallCancellation` arrived. The pending call (`call_1019612`,
  `call_1199193`, `call_975170` in the N=3 runs) was never cancelled, and no
  second `book_slot` call came. The fake booking committed 7.0 s after the
  call, 0.9-1.0 s after `interrupted`. The tool response (`status: booked`)
  went out 0-5 ms later, and the server sent no error or other message in
  reply to it. 0.5-0.9 s after `ACTIVITY_END` for the stop, the model said
  the booking was already made ("It is too late to stop; the booking for
  tomorrow at 3 PM was already made."). That matches the tool response, but
  in text A and B the model said the same kind of thing before any tool
  response had been sent.
- audio_C re-run: `interrupted` arrived 147-262 ms after the stop clip
  started, with no `toolCallCancellation`. Run 2 double-booked again: a
  second `book_slot` call came 355 ms after `ACTIVITY_END`, and both
  bookings committed. In runs 1 and 3, one booking committed and the tool
  response said `booked`. 1256 and 1172 ms after the commit, the model said
  "The booking was not made, so nothing has been scheduled." and "The
  booking was not made as you requested." The model audio of all eight
  sessions is in `results/audio_out/`.

## What this does not establish

- The booking service is a fake in-process `asyncio` job, not a real backend.
  Cancelling it is an `asyncio.Task.cancel()`.
- Scenarios A-F use typed text (`send_realtime_input(text=...)`).
  `--input audio` sends speech, so text-only input is no longer a limitation
  of the harness. The clips are synthetic macOS speech: one voice
  (Samantha), one wording per utterance, no background noise, no room
  acoustics, digital silence between utterances, and no echo of the model's
  own audio. Real users and microphones are not covered.
- With audio input, VAD sits between sending and the server acting on the
  speech. Offsets measured from `stop_sent_at_ms` (the first chunk) include
  VAD start-of-speech latency, and the model's reply includes end-of-speech
  latency after `stop_audio_end_ms`. The text runs had neither.
- It uses the Google AI Studio endpoint with an API key, not Vertex AI. It
  does not cover other models, and it does not cover session resumption.
- N=3 per scenario, plus one smoke session each for G1 and G2, over two
  days: 41 sessions in all, 21 text and 20 audio. That shows what can
  happen, not how often.
- G needed a follow-up question to make the model speak while the call was
  pending. With the G1 wording it did not. Other prompts, voices, or
  longer-running calls may behave differently.
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
- `run_audio.sh`: runs audio_A, audio_C, audio_D, audio_F with
  `--input audio`, N=3, and appends to the summary.
- `run_g.sh`: runs G2 and the audio_C re-run with `--save-audio`, N=3 each,
  and appends to the summary.
- `assets/audio/book.wav`, `assets/audio/stop.wav`: the two utterances as
  16 kHz 16-bit mono PCM WAV (macOS `say`, voice Samantha).
  `book_bring.wav` (G1) and `bring.wav` (G2) were made the same way.
- `introspect.py`: prints the SDK version and the fields and signatures used.
- `.env.example`: `GEMINI_API_KEY=` placeholder. `.env` is git-ignored.
- `results/`: JSONL timelines and `summary.md`. `results/audio_out/`: WAV
  files and sidecars written by `--save-audio`.
