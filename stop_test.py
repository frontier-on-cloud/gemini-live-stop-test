"""Gemini Live API: what happens to an in-flight tool call when the user says "stop"?

One run = one Live session:
  1. send "Book me the 3pm slot tomorrow, please."
  2. on the book_slot tool call, start a fake booking job that commits after --latency s
  3. --stop-after s after the tool call arrived, send "Actually, stop. Don't book it."
  4. record every server event (interrupted, turn_complete, generation_complete,
     tool_call_cancellation, model text/transcript, audio chunks) with a timestamp
     in ms relative to session start (time.monotonic()).

--input text (default) sends the two utterances with send_realtime_input(text=...).
--input audio streams assets/audio/{book,stop}.wav instead: 16 kHz 16-bit mono PCM,
100 ms per send_realtime_input(audio=...) call, paced in real time, with silence
between utterances like an open microphone. Automatic activity detection (VAD) is
left at the server default.

--stop-after-model-speech S (audio input, scenario G) starts the stop clip S seconds
after the first model audio chunk that arrives after the book_slot call, and only if
no tool response has been sent yet, so the stop barges in while the model is speaking
with the call still pending. --save-audio writes the model's output audio of each run
as a WAV plus a JSON sidecar with chunk arrival times and copies of the user clips
(results/audio_out/).

Writes results/<name>.jsonl (one JSON object per event) and appends a Markdown
table to results/summary.md after the N runs. The API key is read only from
GEMINI_API_KEY (.env in this folder or the environment) and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

try:  # websockets is a google-genai dependency
    from websockets.exceptions import ConnectionClosed
except Exception:  # pragma: no cover
    ConnectionClosed = ()  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "gemini-3.8-live"
SDK_VERSION = pkg_version("google-genai")

SYSTEM_INSTRUCTION = (
    "You are a scheduling assistant. When the user asks to book a slot, call "
    "book_slot immediately, then tell the user you are booking it. Keep replies "
    "to one or two short sentences. If the user tells you to stop or cancel, "
    "tell them plainly whether the booking was already made or not."
)
BOOK_TEXT = "Book me the 3pm slot tomorrow, please."
STOP_TEXT = "Actually, stop. Don't book it."

AUDIO_DIR = HERE / "assets" / "audio"
AUDIO_RATE = 16000  # Hz; clips are 16-bit little-endian mono PCM
AUDIO_MIME = f"audio/pcm;rate={AUDIO_RATE}"
CHUNK_S = 0.1  # seconds of audio per send_realtime_input(audio=...) call
MODEL_AUDIO_RATE = 24000  # Hz; used for --save-audio only if the mime type has no rate

# Raw (camelCase) keys this script knows about. Anything else is logged by name.
KNOWN_RAW_TOP = {
    "setupComplete", "serverContent", "toolCall", "toolCallCancellation",
    "goAway", "sessionResumptionUpdate", "usageMetadata", "voiceActivity",
    "voiceActivityDetectionSignal",
}
KNOWN_RAW_SERVER_CONTENT = {
    "modelTurn", "turnComplete", "generationComplete", "interrupted",
    "outputTranscription", "inputTranscription", "turnCompleteReason",
    "waitingForInput", "interactionStatus", "interimInputTranscription",
}

# ---------------------------------------------------------------- redaction --

_SECRETS: list[str] = []
_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_KEY_PARAM_RE = re.compile(r"((?:api[_-]?)?key=)[^&\s'\"]+", re.IGNORECASE)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _KEY_RE.sub("[REDACTED]", text)
    return _KEY_PARAM_RE.sub(r"\1[REDACTED]", text)


def err_text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:500]


# ------------------------------------------------------------------ helpers --


class Clock:
    """Milliseconds since session start, from time.monotonic()."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def ms(self) -> int:
        return int(round((time.monotonic() - self.t0) * 1000))


class JsonlWriter:
    def __init__(self, path: Path, scenario: str, verbose: bool) -> None:
        self.path = path
        self.scenario = scenario
        self.verbose = verbose
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def write(self, run: int, t_ms: int, event: str, **fields: Any) -> None:
        obj = {"scenario": self.scenario, "run": run, "t_ms": t_ms, "event": event}
        obj.update(fields)
        line = redact(json.dumps(obj, ensure_ascii=False, default=str))
        self._fh.write(line + "\n")
        self._fh.flush()
        if self.verbose:
            print("   ", line[:300])

    def close(self) -> None:
        self._fh.close()


class BookingService:
    """Fake external service. A job commits `latency_s` after it starts."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.committed: list[dict] = []
        self.jobs: dict[str, asyncio.Task] = {}
        self._n = 0

    async def book(self, slot: str, latency_s: float) -> dict:
        await asyncio.sleep(latency_s)
        record = {"slot": slot, "committed_at": self.clock.ms()}
        self.committed.append(record)
        return record

    def start(self, slot: str, latency_s: float) -> str:
        self._n += 1
        job_id = f"job-{self._n}"
        self.jobs[job_id] = asyncio.create_task(self.book(slot, latency_s))
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Abort a pending job. False if unknown or already committed."""
        task = self.jobs.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True


class WsTap:
    """Wraps the SDK's websocket to see raw key names the SDK may drop."""

    def __init__(self, ws: Any, state: "RunState") -> None:
        self._ws = ws
        self._state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    async def recv(self, *args: Any, **kwargs: Any) -> Any:
        try:
            raw = await self._ws.recv(*args, **kwargs)
        except ConnectionClosed as exc:  # type: ignore[misc]
            self._state.on_ws_closed(exc)
            raise
        self._state.on_raw(raw)
        return raw


# -------------------------------------------------------------------- audio --


def load_pcm(path: Path) -> bytes:
    """Raw PCM frames of a 16 kHz, 16-bit, mono, uncompressed WAV file."""
    with wave.open(str(path), "rb") as w:
        fmt = (w.getframerate(), w.getsampwidth() * 8, w.getnchannels(), w.getcomptype())
        if fmt != (AUDIO_RATE, 16, 1, "NONE"):
            raise ValueError(f"{path.name}: expected (16000 Hz, 16 bit, 1 ch, NONE), got {fmt}")
        return w.readframes(w.getnframes())


def pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / (AUDIO_RATE * 2)


@dataclass
class Utterance:
    label: str
    chunks: list[bytes]
    started: asyncio.Future  # ms of the first chunk sent, or None if sending failed
    ended: asyncio.Future    # ms of the last chunk sent, or None if sending failed


class Mic:
    """Simulated open microphone for --input audio.

    Every send is one send_realtime_input(audio=Blob(pcm, "audio/pcm;rate=16000"))
    call carrying CHUNK_S of audio, paced in real time against an absolute
    schedule (each send waits for the duration of the previous chunk). While no
    utterance is queued it sends silence, so the server's automatic VAD can
    detect the end of speech. An utterance queued while the mic is idle starts on
    the very next send (the pacing grid restarts there), so the stop clip starts
    when the --stop-after timer fires.
    """

    def __init__(self, session: Any, st: "RunState") -> None:
        self.session = session
        self.st = st
        self.chunk_bytes = int(AUDIO_RATE * CHUNK_S) * 2
        self.silence = bytes(self.chunk_bytes)
        self.queue: deque[Utterance] = deque()
        self.wake = asyncio.Event()
        self.speech_chunks = 0
        self.silence_chunks = 0

    def say(self, label: str, pcm: bytes) -> Utterance:
        loop = asyncio.get_running_loop()
        chunks = [pcm[i:i + self.chunk_bytes] for i in range(0, len(pcm), self.chunk_bytes)]
        utt = Utterance(label, chunks, loop.create_future(), loop.create_future())
        self.queue.append(utt)
        self.wake.set()
        return utt

    def _fail_pending(self, current: Utterance | None) -> None:
        for utt in ([current] if current else []) + list(self.queue):
            for fut in (utt.started, utt.ended):
                if not fut.done():
                    fut.set_result(None)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        st = self.st
        current: Utterance | None = None
        idx = 0
        next_t = loop.time()
        while not (st.closed or st.ending):
            self.wake.clear()
            if current is None and self.queue:
                current, idx = self.queue.popleft(), 0
            data = current.chunks[idx] if current else self.silence
            try:
                await self.session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type=AUDIO_MIME))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not (st.closed or st.ending):
                    st.error("send_audio", err_text(exc))
                self._fail_pending(current)
                return
            if current is None:
                self.silence_chunks += 1
            else:
                self.speech_chunks += 1
                if idx == 0:
                    t = st.emit("user_audio_start", label=current.label,
                                audio_s=round(sum(map(len, current.chunks)) / (AUDIO_RATE * 2), 3),
                                chunks=len(current.chunks), chunk_ms=int(CHUNK_S * 1000),
                                mime_type=AUDIO_MIME)
                    st.utterances.append({"label": current.label, "start_ms": t})
                    current.started.set_result(t)
                idx += 1
                if idx == len(current.chunks):
                    t = st.emit("user_audio_end", label=current.label,
                                since_start_ms=t_ms_since(st, current.started))
                    st.utterances[-1]["end_ms"] = t
                    current.ended.set_result(t)
                    current = None
            next_t += len(data) / (AUDIO_RATE * 2)
            now = loop.time()
            if next_t < now - 0.5:  # event loop stalled: resync instead of bursting
                next_t = now
            delay = next_t - now
            if current is None and not self.queue:
                try:
                    await asyncio.wait_for(self.wake.wait(), max(0.0, delay))
                    next_t = loop.time()  # utterance queued while idle: start now
                except TimeoutError:
                    pass
            elif delay > 0:
                await asyncio.sleep(delay)


def t_ms_since(st: "RunState", started: asyncio.Future) -> int | None:
    t0 = started.result() if started.done() else None
    return None if t0 is None else st.clock.ms() - t0


# -------------------------------------------------------------------- state --


@dataclass
class RunState:
    args: argparse.Namespace
    run: int
    clock: Clock
    out: JsonlWriter
    modality: str
    service: BookingService = field(init=False)
    send_method: str | None = None
    first_call_id: str | None = None
    tool_call_at_ms: int | None = None
    stop_sent_at_ms: int | None = None  # audio: first chunk of the stop clip
    stop_audio_end_ms: int | None = None  # audio: last chunk of the stop clip
    no_tool_call: bool = False
    done_reason: str = "unknown"
    closed: bool = False
    ending: bool = False
    tool_calls: list[dict] = field(default_factory=list)
    call_jobs: dict[str, str] = field(default_factory=dict)  # call id -> job id
    cancelled_ids: set[str] = field(default_factory=set)
    interrupted_ms: list[int] = field(default_factory=list)
    cancellations: list[dict] = field(default_factory=list)
    cancel_attempts: list[dict] = field(default_factory=list)
    job_outcomes: list[dict] = field(default_factory=list)
    turn_complete_ms: list[int] = field(default_factory=list)
    tool_responses: list[dict] = field(default_factory=list)
    texts: list[tuple[int, int, str]] = field(default_factory=list)  # (ms, turn, text)
    turn_idx: int = 0
    errors: list[str] = field(default_factory=list)
    pending_at_end: list[str] = field(default_factory=list)
    not_started: list[str] = field(default_factory=list)  # call ids cancelled before arrival
    last_raw_unknown_only: bool = False
    last_output_ms: int = -1
    audio_seg: dict | None = None
    audio_chunks_total: int = 0
    audio_bytes_total: int = 0
    tool_call_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)
    mic: Mic | None = None
    utterances: list[dict] = field(default_factory=list)  # audio: label, start_ms, end_ms
    vad_events: list[dict] = field(default_factory=list)
    input_texts: list[tuple[int, str]] = field(default_factory=list)
    wall: str = ""
    first_audio_ms: int | None = None        # first model audio chunk of the session
    speech_after_call_ms: int | None = None  # first model audio chunk after the first tool call
    model_speech_event: asyncio.Event = field(default_factory=asyncio.Event)
    tool_response_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_skipped: str | None = None  # --stop-after-model-speech: why no stop was sent
    stop_skipped_ms: int | None = None
    pending_at_stop: list[str] = field(default_factory=list)  # call ids pending at the stop
    generation_complete_ms: list[int] = field(default_factory=list)
    audio_out: list[dict] = field(default_factory=list)  # --save-audio: per-chunk metadata
    audio_data: list[bytes] = field(default_factory=list)  # --save-audio: chunk bytes

    def __post_init__(self) -> None:
        self.service = BookingService(self.clock)

    def emit(self, event: str, **fields: Any) -> int:
        t = self.clock.ms()
        self.out.write(self.run, t, event, **fields)
        return t

    def error(self, where: str, detail: str) -> None:
        self.errors.append(f"{where}: {detail}")
        self.emit("error", where=where, detail=detail)

    # raw tap callbacks
    def on_raw(self, raw: Any) -> None:
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            self.emit("raw_unparseable", size=len(raw) if raw else 0)
            return
        if not isinstance(data, dict):
            self.emit("raw_non_object", type=type(data).__name__)
            return
        if "error" in data and isinstance(data["error"], dict):
            e = data["error"]
            self.error("server_error_message",
                       f"code={e.get('code')} status={e.get('status')} "
                       f"message={redact(str(e.get('message')))[:300]}")
        for key in ("voiceActivity", "voiceActivityDetectionSignal"):
            if key in data:
                self.emit("raw_vad", key=key, payload=data[key])
        unknown = sorted(set(data) - KNOWN_RAW_TOP - {"error"})
        self.last_raw_unknown_only = bool(unknown) and len(unknown) == len(data)
        if unknown:
            self.emit("raw_unknown_keys", keys=unknown)
        sc = data.get("serverContent")
        if isinstance(sc, dict):
            unknown_sc = sorted(set(sc) - KNOWN_RAW_SERVER_CONTENT)
            if unknown_sc:
                self.emit("raw_unknown_server_content_keys", keys=unknown_sc)

    def on_ws_closed(self, exc: BaseException) -> None:
        if self.closed:
            return
        self.closed = True
        rcvd = getattr(exc, "rcvd", None)
        code = getattr(rcvd, "code", None)
        reason = redact(str(getattr(rcvd, "reason", "") or ""))[:300]
        self.emit("ws_closed", code=code, reason=reason, during_run_end=self.ending)
        if not self.ending:
            self.errors.append(f"ws_closed code={code} {reason}".strip())

    # audio accounting: one summary event per contiguous segment
    def pending_call_ids(self) -> list[str]:
        """book_slot calls with no tool response sent whose job was not cancelled."""
        responded = {r["call_id"] for r in self.tool_responses}
        cancelled = {o["call_id"] for o in self.job_outcomes if o["outcome"] == "cancelled"}
        return [c["id"] for c in self.tool_calls if c["name"] == "book_slot"
                and c["id"] not in responded and c["id"] not in cancelled
                and c["id"] not in self.not_started]

    def audio_chunk(self, data: bytes, mime: str) -> None:
        nbytes = len(data)
        t = self.clock.ms()
        self.audio_chunks_total += 1
        self.audio_bytes_total += nbytes
        if self.audio_seg is None:
            self.audio_seg = {"first_ms": t, "last_ms": t, "chunks": 0, "bytes": 0}
            self.emit("audio_start")
        self.audio_seg["last_ms"] = t
        self.audio_seg["chunks"] += 1
        self.audio_seg["bytes"] += nbytes
        self.last_output_ms = t
        if self.first_audio_ms is None:
            self.first_audio_ms = t
        if self.tool_call_at_ms is not None and self.speech_after_call_ms is None:
            self.speech_after_call_ms = t
            self.emit("model_speech_after_tool_call", since_tool_call_ms=t - self.tool_call_at_ms,
                      pending_call_ids=self.pending_call_ids(),
                      tool_response_already_sent=bool(self.tool_responses))
            self.model_speech_event.set()
        if self.args.save_audio:
            self.audio_out.append({"t_ms": t, "bytes": nbytes, "mime_type": mime,
                                   "turn": self.turn_idx})
            self.audio_data.append(data)

    def flush_audio(self, reason: str) -> None:
        if self.audio_seg is not None:
            self.emit("audio_segment", closed_by=reason, **self.audio_seg)
            self.audio_seg = None

    def post_stop_ref(self) -> int | None:
        """Where the post-stop windows start: the stop message (text input) or the
        last chunk of the stop clip (audio input; the model cannot answer before it)."""
        if self.args.input == "audio":
            return self.stop_audio_end_ms
        return self.stop_sent_at_ms

    def settled(self, now: int) -> bool:
        """Stop condition after the stop message (see README)."""
        ref = self.post_stop_ref()
        if ref is None:
            return False
        if now - ref < self.args.min_post_stop * 1000:
            return False
        if any(not t.done() for t in self.service.jobs.values()):
            return False
        # The model must have replied to the stop. A turn_complete that only closes
        # the interrupted turn (no output after the stop) does not count.
        if self.last_output_ms < ref:
            return False
        last_tc = max(self.turn_complete_ms, default=-1)
        if last_tc < ref:
            return False
        if self.last_output_ms > last_tc:  # model is mid-turn
            return False
        last_resp = max((r["at_ms"] for r in self.tool_responses), default=-1)
        if last_resp > last_tc:  # give the model a chance to react to the response
            return now - max(last_resp, self.last_output_ms) >= self.args.quiet * 1000
        return True

    def settled_without_stop(self, now: int) -> bool:
        """--stop-after-model-speech skipped the stop: end once every job is done, the
        model is not mid-turn, and --quiet s passed since the last output or response."""
        if any(not t.done() for t in self.service.jobs.values()):
            return False
        if self.last_output_ms > max(self.turn_complete_ms, default=-1):
            return False
        last = max([self.stop_skipped_ms or 0, self.last_output_ms]
                   + [r["at_ms"] for r in self.tool_responses])
        return now - last >= self.args.quiet * 1000


# ------------------------------------------------------------------- config --


def build_config(args: argparse.Namespace, modality: str) -> types.LiveConnectConfig:
    decl: dict[str, Any] = dict(
        name="book_slot",
        description="Book an appointment slot for the user. Returns a confirmation.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"slot": types.Schema(
                type=types.Type.STRING,
                description="The slot to book, e.g. 'tomorrow 3pm'.")},
            required=["slot"],
        ),
    )
    if args.behavior != "unset":
        decl["behavior"] = types.Behavior(args.behavior)
    cfg: dict[str, Any] = dict(
        response_modalities=[types.Modality(modality)],
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM_INSTRUCTION)]),
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**decl)])],
    )
    if modality == "AUDIO":
        cfg["output_audio_transcription"] = types.AudioTranscriptionConfig()
    if args.input == "audio":
        # Logging only: shows what the server heard. realtime_input_config is not
        # set, so automatic activity detection stays at the server default.
        cfg["input_audio_transcription"] = types.AudioTranscriptionConfig()
    return types.LiveConnectConfig(**cfg)


def tool_response(args: argparse.Namespace, fc_id: str, name: str, payload: dict
                  ) -> types.FunctionResponse:
    kwargs: dict[str, Any] = {"id": fc_id, "name": name, "response": dict(payload)}
    if args.scheduling != "none":
        if args.scheduling_in in ("field", "both"):
            kwargs["scheduling"] = types.FunctionResponseScheduling(args.scheduling)
        if args.scheduling_in in ("response", "both"):
            kwargs["response"]["scheduling"] = args.scheduling
    return types.FunctionResponse(**kwargs)


# ------------------------------------------------------------- run pieces ----


async def send_user_text(session: Any, st: RunState, text: str, method: str,
                         label: str) -> int:
    if method == "realtime":
        await session.send_realtime_input(text=text)
    else:
        await session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=text)])],
            turn_complete=True)
    return st.emit("user_text_sent", label=label, method=method, text=text)


async def respond_when_done(session: Any, st: RunState, fc_id: str, name: str,
                            slot: str, job_id: str) -> None:
    task = st.service.jobs[job_id]
    await asyncio.wait([task])
    if task.cancelled():
        why = "run_end" if st.ending else "honor_cancel"
        st.job_outcomes.append({"job_id": job_id, "call_id": fc_id, "outcome": "cancelled",
                                "at_ms": st.clock.ms(), "why": why})
        st.emit("service_job_cancelled", job_id=job_id, call_id=fc_id, why=why)
        return
    record = task.result()
    st.job_outcomes.append({"job_id": job_id, "call_id": fc_id, "outcome": "committed",
                            "at_ms": record["committed_at"]})
    st.emit("service_committed", job_id=job_id, call_id=fc_id, **record,
            call_was_cancelled_by_server=fc_id in st.cancelled_ids)
    if st.args.respond != "immediate" or st.closed or st.ending:
        return
    fr = tool_response(st.args, fc_id, name, {
        "status": "booked", "slot": record["slot"],
        "confirmation_id": f"BK-{1000 + len(st.job_outcomes)}"})
    try:
        await session.send_tool_response(function_responses=[fr])
        t = st.emit("tool_response_sent", call_id=fc_id,
                    call_was_cancelled_by_server=fc_id in st.cancelled_ids,
                    scheduling=st.args.scheduling, scheduling_in=st.args.scheduling_in,
                    response=fr.response)
        st.tool_responses.append({"at_ms": t, "call_id": fc_id})
        st.tool_response_event.set()
    except Exception as exc:
        st.error("send_tool_response", err_text(exc))


def handle_tool_call(session: Any, st: RunState, msg: types.LiveServerMessage) -> None:
    for fc in msg.tool_call.function_calls or []:
        args = dict(fc.args or {})
        first = st.first_call_id is None
        t = st.emit("tool_call_received", call_id=fc.id, name=fc.name, args=args,
                    first=first)
        st.tool_calls.append({"id": fc.id, "name": fc.name, "at_ms": t})
        if fc.name != "book_slot":
            fr = types.FunctionResponse(id=fc.id, name=fc.name,
                                        response={"error": f"unknown function {fc.name}"})
            st.tasks.append(asyncio.create_task(
                session.send_tool_response(function_responses=[fr])))
            continue
        if first:
            st.first_call_id = fc.id
            st.tool_call_at_ms = t
            st.tool_call_event.set()
        slot = str(args.get("slot", "")) or "unspecified"
        if fc.id in st.cancelled_ids and st.args.honor_cancel:
            st.not_started.append(fc.id)
            st.emit("job_not_started", call_id=fc.id, reason="id already cancelled by server")
            continue
        job_id = st.service.start(slot, st.args.latency)
        st.call_jobs[fc.id] = job_id
        st.emit("service_job_started", job_id=job_id, call_id=fc.id, slot=slot,
                latency_s=st.args.latency)
        st.tasks.append(asyncio.create_task(
            respond_when_done(session, st, fc.id, fc.name, slot, job_id)))


def handle_cancellation(st: RunState, msg: types.LiveServerMessage) -> None:
    ids = list(msg.tool_call_cancellation.ids or [])
    st.cancelled_ids.update(ids)
    t = st.emit("tool_call_cancellation", ids=ids,
                matches_first_call=st.first_call_id in ids)
    st.cancellations.append({"at_ms": t, "ids": ids,
                             "match": st.first_call_id in ids})
    if not st.args.honor_cancel:
        return
    for cid in ids:
        job_id = st.call_jobs.get(cid)
        if job_id is None:
            result = "unknown_call_id"
        elif st.service.cancel(job_id):
            result = "cancelled_before_commit"
        else:
            result = "too_late_already_committed"
        st.cancel_attempts.append({"call_id": cid, "result": result, "at_ms": st.clock.ms()})
        st.emit("cancel_attempt", call_id=cid, job_id=job_id, result=result)


def handle_server_content(st: RunState, sc: types.LiveServerContent) -> None:
    extra = {}
    for name in ("turn_complete_reason", "waiting_for_input", "interaction_status"):
        val = getattr(sc, name, None)
        if val is not None:
            extra[name] = val
    if sc.model_turn and sc.model_turn.parts:
        for part in sc.model_turn.parts:
            if part.inline_data is not None and (part.inline_data.mime_type or "").startswith("audio"):
                st.audio_chunk(part.inline_data.data or b"", part.inline_data.mime_type or "")
            elif part.text:
                if part.thought:
                    st.emit("model_thought", text=part.text[:400])
                else:
                    t = st.emit("model_text", text=part.text)
                    st.texts.append((t, st.turn_idx, part.text))
                    st.last_output_ms = t
            else:
                set_fields = sorted(k for k, v in part if v is not None)
                st.emit("unhandled_part", fields=set_fields)
    if sc.output_transcription and sc.output_transcription.text:
        t = st.emit("model_transcript", text=sc.output_transcription.text)
        st.texts.append((t, st.turn_idx, sc.output_transcription.text))
        st.last_output_ms = t
    if sc.input_transcription and sc.input_transcription.text:
        t = st.emit("input_transcript", text=sc.input_transcription.text)
        st.input_texts.append((t, sc.input_transcription.text))
    iit = getattr(sc, "interim_input_transcription", None)
    if iit is not None and iit.text:
        st.emit("interim_input_transcript", text=iit.text)
    if sc.interrupted:
        st.flush_audio("interrupted")
        t = st.emit("interrupted", **extra)
        st.interrupted_ms.append(t)
        st.turn_idx += 1
    if sc.generation_complete:
        st.flush_audio("generation_complete")
        st.generation_complete_ms.append(st.emit("generation_complete", **extra))
    if sc.turn_complete:
        st.flush_audio("turn_complete")
        t = st.emit("turn_complete", **extra)
        st.turn_complete_ms.append(t)
        st.turn_idx += 1
    elif extra and not (sc.interrupted or sc.generation_complete):
        st.emit("server_content_status", **extra)
    handled = {"model_turn", "output_transcription", "input_transcription", "interrupted",
               "generation_complete", "turn_complete", "turn_complete_reason",
               "waiting_for_input", "interaction_status", "interim_input_transcription"}
    other = sorted(k for k, v in sc if v is not None and k not in handled)
    if other:
        st.emit("unhandled_server_content", fields=other)


def enum_str(v: Any) -> str | None:
    return None if v is None else str(getattr(v, "value", v))


async def receiver(session: Any, st: RunState) -> None:
    handled = {"server_content", "tool_call", "tool_call_cancellation", "go_away",
               "usage_metadata", "setup_complete", "session_resumption_update",
               "voice_activity", "voice_activity_detection_signal"}
    consecutive_errors = 0
    while not st.closed:
        try:
            # session.receive() stops after each completed turn, so loop around it.
            async for msg in session.receive():
                consecutive_errors = 0
                if msg.server_content:
                    handle_server_content(st, msg.server_content)
                if msg.tool_call:
                    handle_tool_call(session, st, msg)
                if msg.tool_call_cancellation:
                    handle_cancellation(st, msg)
                if msg.go_away:
                    st.emit("go_away", time_left=str(msg.go_away.time_left))
                if msg.usage_metadata:
                    um = msg.usage_metadata
                    st.emit("usage_metadata", total_token_count=um.total_token_count,
                            prompt_token_count=um.prompt_token_count,
                            response_token_count=um.response_token_count)
                if msg.setup_complete:
                    st.emit("setup_complete_message")
                if msg.session_resumption_update:
                    st.emit("session_resumption_update")
                if msg.voice_activity:
                    va = msg.voice_activity
                    kind = enum_str(va.voice_activity_type)
                    t = st.emit("voice_activity", voice_activity_type=kind,
                                audio_offset=va.audio_offset)
                    st.vad_events.append({"at_ms": t, "type": kind,
                                          "audio_offset": va.audio_offset})
                if msg.voice_activity_detection_signal:
                    kind = enum_str(msg.voice_activity_detection_signal.vad_signal_type)
                    t = st.emit("vad_signal", vad_signal_type=kind)
                    st.vad_events.append({"at_ms": t, "type": kind})
                unhandled = sorted(k for k, v in msg if v is not None and k not in handled)
                if unhandled:
                    st.emit("unhandled_message", type=type(msg).__name__, fields=unhandled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if isinstance(exc, errors.APIError) and isinstance(code, int) and 1000 <= code < 5000:
                if not st.closed:
                    st.closed = True
                    st.emit("ws_closed", code=code, reason=err_text(exc),
                            during_run_end=st.ending)
                    if not st.ending:
                        st.errors.append(f"ws_closed {err_text(exc)[:200]}")
                return
            hint = ("SDK failed on a message with only unknown keys "
                    "(raw_unknown_keys): " if st.last_raw_unknown_only else "")
            st.error("receive", hint + err_text(exc))
            consecutive_errors += 1
            if st.closed or consecutive_errors >= 5:
                return
            await asyncio.sleep(0.05)


def skip_stop(st: RunState, reason: str) -> None:
    st.stop_skipped = reason
    st.stop_skipped_ms = st.emit("stop_skipped", reason=reason,
                                 pending_call_ids=st.pending_call_ids())


async def wait_for_model_speech(st: RunState) -> int | None:
    """Anchor for --stop-after-model-speech: the first model audio chunk after the tool
    call. Returns None (stop skipped) if the tool response goes out first, or if no
    model audio arrives within --tool-call-wait s."""
    if st.speech_after_call_ms is None and not st.tool_responses:
        waits = [asyncio.create_task(st.model_speech_event.wait()),
                 asyncio.create_task(st.tool_response_event.wait())]
        _, pending = await asyncio.wait(waits, timeout=st.args.tool_call_wait,
                                        return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    if st.speech_after_call_ms is not None:
        return st.speech_after_call_ms
    skip_stop(st, "tool_response_sent_before_model_speech" if st.tool_responses
              else f"no_model_speech_within_{st.args.tool_call_wait}s_after_tool_call")
    return None


async def user_script_audio(session: Any, st: RunState) -> None:
    """--input audio: both utterances go through the simulated open mic.

    --stop-after counts from the tool call to the FIRST chunk of the stop clip.
    --stop-after-request counts from the LAST chunk of the booking clip. There is
    no send_client_content fallback in audio mode.
    """
    a = st.args
    mic = Mic(session, st)
    st.mic = mic
    st.tasks.append(asyncio.create_task(mic.run()))
    st.send_method = "realtime_audio"
    book = mic.say("book_request", a.clips["book"])
    book_end = await book.ended
    if book_end is None:
        st.no_tool_call = True
        return
    if a.stop_after_request is not None:
        target = book_end + a.stop_after_request * 1000
    else:
        try:
            await asyncio.wait_for(st.tool_call_event.wait(), a.tool_call_wait)
        except TimeoutError:
            pass
        if st.tool_call_at_ms is None:
            st.no_tool_call = True
            st.emit("no_tool_call", waited_s=a.tool_call_wait, counted_from="book clip end")
            return
        if a.followup_audio is not None:
            target = st.tool_call_at_ms + a.followup_after_tool_call * 1000
            await asyncio.sleep(max(0.0, (target - st.clock.ms()) / 1000))
            mic.say("followup", a.clips["followup"])
            st.emit("followup_queued", since_tool_call_ms=st.clock.ms() - st.tool_call_at_ms,
                    pending_call_ids=st.pending_call_ids())
        if a.stop_after_model_speech is not None:
            anchor = await wait_for_model_speech(st)
            if anchor is None:
                return
            target = anchor + a.stop_after_model_speech * 1000
        else:
            target = st.tool_call_at_ms + a.stop_after * 1000
    await asyncio.sleep(max(0.0, (target - st.clock.ms()) / 1000))
    if a.stop_after_model_speech is not None and st.tool_responses:
        skip_stop(st, "tool_response_sent_before_stop")
        return
    stop = mic.say("stop", a.clips["stop"])
    started = await stop.started
    if started is None:  # send failed (already logged)
        st.stop_sent_at_ms = st.stop_audio_end_ms = st.clock.ms()
        return
    st.stop_sent_at_ms = started
    st.pending_at_stop = st.pending_call_ids()
    if a.stop_after_request is not None:
        st.emit("stop_sent", since_book_audio_end_ms=started - book_end,
                tool_call_already_received=st.tool_call_at_ms is not None)
    elif a.stop_after_model_speech is not None:
        st.emit("stop_sent", since_model_speech_ms=started - st.speech_after_call_ms,
                since_tool_call_ms=started - st.tool_call_at_ms,
                pending_call_ids=st.pending_at_stop,
                tool_response_already_sent=bool(st.tool_responses))
    else:
        st.emit("stop_sent", since_tool_call_ms=started - st.tool_call_at_ms)
    ended = await stop.ended
    st.stop_audio_end_ms = ended if ended is not None else st.clock.ms()


async def user_script(session: Any, st: RunState) -> None:
    a = st.args
    if a.input == "audio":
        await user_script_audio(session, st)
        return
    first = "realtime" if a.send_method == "auto" else a.send_method
    first_ok = True
    req_ms = st.clock.ms()
    try:
        req_ms = await send_user_text(session, st, BOOK_TEXT, first, "book_request")
    except Exception as exc:
        first_ok = False
        st.error(f"send_{first}", err_text(exc))
        if a.send_method != "auto" or a.stop_after_request is not None:
            st.no_tool_call = True
            return
    if a.stop_after_request is not None:
        # Stop timer starts at the request, not at the tool call. No fallback resend.
        st.send_method = first
        target = req_ms + a.stop_after_request * 1000
        await asyncio.sleep(max(0.0, (target - st.clock.ms()) / 1000))
        try:
            st.stop_sent_at_ms = await send_user_text(session, st, STOP_TEXT, first, "stop")
            st.emit("stop_sent", since_request_ms=st.stop_sent_at_ms - req_ms,
                    tool_call_already_received=st.tool_call_at_ms is not None)
        except Exception as exc:
            st.error("send_stop", err_text(exc))
            st.stop_sent_at_ms = st.clock.ms()
        return
    method = first
    try:
        if not first_ok:
            raise TimeoutError
        await asyncio.wait_for(st.tool_call_event.wait(), a.tool_call_wait)
    except TimeoutError:
        if a.send_method == "auto" and not st.closed:
            reason = (f"no tool call within {a.tool_call_wait}s via realtime" if first_ok
                      else "send_realtime_input failed")
            st.emit("send_fallback", reason=reason)
            method = "client_content"
            try:
                await send_user_text(session, st, BOOK_TEXT, method, "book_request_fallback")
                await asyncio.wait_for(st.tool_call_event.wait(), a.tool_call_wait)
            except TimeoutError:
                pass
            except Exception as exc:
                st.error("send_client_content", err_text(exc))
    if st.tool_call_at_ms is None:
        st.no_tool_call = True
        st.emit("no_tool_call")
        return
    st.send_method = method if method == first else "client_content (fallback)"
    target = st.tool_call_at_ms + a.stop_after * 1000
    await asyncio.sleep(max(0.0, (target - st.clock.ms()) / 1000))
    try:
        st.stop_sent_at_ms = await send_user_text(session, st, STOP_TEXT, method, "stop")
        st.emit("stop_sent", since_tool_call_ms=st.stop_sent_at_ms - st.tool_call_at_ms)
    except Exception as exc:
        st.error("send_stop", err_text(exc))
        st.stop_sent_at_ms = st.clock.ms()


async def wait_until_done(st: RunState) -> str:
    while True:
        if st.closed:
            return "ws_closed"
        if st.no_tool_call:
            return "no_tool_call"
        if st.stop_skipped is not None:
            now = st.clock.ms()
            if st.settled_without_stop(now):
                return "stop_skipped_settled"
            if now - st.stop_skipped_ms >= st.args.post_stop_window * 1000:
                return "stop_skipped_window_elapsed"
        ref = st.post_stop_ref()
        if ref is not None:
            now = st.clock.ms()
            if now - ref >= st.args.post_stop_window * 1000:
                return "post_stop_window_elapsed"
            if st.settled(now):
                return "settled"
        await asyncio.sleep(0.05)


# ----------------------------------------------------------------- one run --


QUOTA_RE = re.compile(r"quota|billing|RESOURCE_EXHAUSTED|exceeded your current|prepay|"
                      r"credits?\b|payment|\b429\b", re.IGNORECASE)
EXIT_QUOTA = 3


def quota_error(st: RunState) -> str | None:
    return next((e for e in st.errors if QUOTA_RE.search(e)), None)


def looks_like_modality_rejection(st: RunState) -> bool:
    if st.modality != "TEXT" or st.tool_calls or st.texts or st.audio_chunks_total:
        return False
    return any("modalit" in e.lower() or re.search(r"\b1007\b", e) for e in st.errors)


async def run_once(args: argparse.Namespace, run: int, modality: str, out: JsonlWriter,
                   connect: Callable[..., Any]) -> RunState:
    clock = Clock()
    st = RunState(args=args, run=run, clock=clock, out=out, modality=modality)
    st.wall = datetime.now().isoformat(timespec="seconds")
    st.emit("run_start", wall=st.wall,
            model=args.model, sdk=SDK_VERSION, behavior=args.behavior,
            stop_after_s=args.stop_after, stop_after_request_s=args.stop_after_request,
            stop_after_model_speech_s=args.stop_after_model_speech,
            followup_after_tool_call_s=(args.followup_after_tool_call
                                        if args.followup_audio is not None else None),
            save_audio=args.save_audio,
            min_post_stop_s=args.min_post_stop, latency_s=args.latency,
            honor_cancel=args.honor_cancel, scheduling=args.scheduling,
            scheduling_in=args.scheduling_in, respond=args.respond,
            modality=modality, send_method_arg=args.send_method, input=args.input,
            **({"clips_s": {k: round(pcm_seconds(v), 3) for k, v in args.clips.items()},
                "clip_files": {k: Path(v).name for k, v in args.audio_paths.items()},
                "chunk_ms": int(CHUNK_S * 1000), "mime_type": AUDIO_MIME}
               if args.input == "audio" else {}))
    done_reason = "unknown"
    try:
        async with asyncio.timeout(args.run_timeout):
            async with connect(model=args.model, config=build_config(args, modality)) as session:
                st.emit("setup_complete",
                        sdk_saw_setup_complete=getattr(session, "setup_complete", None) is not None)
                if hasattr(session, "_ws"):
                    session._ws = WsTap(session._ws, st)
                recv_task = asyncio.create_task(receiver(session, st))
                script_task = asyncio.create_task(user_script(session, st))
                st.tasks += [recv_task, script_task]
                done_reason = await wait_until_done(st)
                st.emit("run_stop_condition", reason=done_reason)
                st.ending = True
                for t in st.tasks:
                    t.cancel()
                await asyncio.gather(*st.tasks, return_exceptions=True)
        st.emit("session_closed")
    except TimeoutError:
        done_reason = "run_timeout"
        st.error("run_timeout", f"hard timeout {args.run_timeout}s")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        done_reason = "exception"
        st.error("session", err_text(exc))
    finally:
        st.ending = True
        for t in st.tasks:
            t.cancel()
        st.pending_at_end = [j for j, t in st.service.jobs.items() if not t.done()]
        if st.pending_at_end:
            st.emit("jobs_pending_at_run_end", job_ids=st.pending_at_end)
        for t in st.service.jobs.values():
            t.cancel()
        await asyncio.gather(*st.tasks, *st.service.jobs.values(), return_exceptions=True)
        st.flush_audio("run_end")
    st.done_reason = done_reason
    return st


# ----------------------------------------------------------------- summary --


def md(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip()


def rel(ms: int, st: RunState) -> str:
    if st.stop_sent_at_ms is None:
        return str(ms)
    return f"{ms} ({ms - st.stop_sent_at_ms:+d} vs stop)"


def join_turns(chunks: list[tuple[int, int, str]]) -> str:
    """Concatenate text chunks within a turn; separate turns with ' / '."""
    turns: dict[int, str] = {}
    for _, turn, txt in chunks:
        turns[turn] = turns.get(turn, "") + txt
    return " / ".join(re.sub(r"\s+", " ", v).strip() for v in turns.values() if v.strip())


def turns_with_times(chunks: list[tuple[int, int, str]]) -> list[dict]:
    """One entry per turn: time of its first text chunk and the joined text."""
    turns: dict[int, dict] = {}
    for t, turn, txt in chunks:
        d = turns.setdefault(turn, {"start_ms": t, "turn": turn, "text": ""})
        d["text"] += txt
    return [dict(d, text=re.sub(r"\s+", " ", d["text"]).strip())
            for d in turns.values() if d["text"].strip()]


def cancellation_ids_match(st: RunState) -> str:
    """Do the toolCallCancellation ids name the call(s) pending when the stop started?"""
    if not st.cancellations:
        return "n/a (no cancellation)"
    ids = {i for c in st.cancellations for i in c["ids"]}
    pending = set(st.pending_at_stop) or ({st.first_call_id} if st.first_call_id else set())
    hit = sorted(ids & pending)
    if hit:
        return "yes (" + ", ".join(hit) + ")"
    return f"no (cancelled {', '.join(sorted(ids))}; pending {', '.join(sorted(pending)) or '-'})"


def summarize(st: RunState) -> dict:
    a = st.args
    stop = st.stop_sent_at_ms
    said_after = join_turns([c for c in st.texts if stop is not None and c[0] >= stop])
    said_before = join_turns([c for c in st.texts if stop is None or c[0] < stop])
    if st.interrupted_ms:
        interrupted = ", ".join(rel(t, st) for t in st.interrupted_ms)
    else:
        interrupted = "no"
    if st.cancellations:
        c = st.cancellations[0]
        call_ids = {tc["id"] for tc in st.tool_calls}
        match = bool(set(c["ids"]) & call_ids)
        cancellation = f"{rel(c['at_ms'], st)}, ids match: {'yes' if match else 'no'}"
        if len(st.cancellations) > 1:
            cancellation += f" (+{len(st.cancellations) - 1} more)"
    else:
        cancellation = "no"
    commits = [o for o in st.job_outcomes if o["outcome"] == "committed"]
    cancels = [o for o in st.job_outcomes if o["outcome"] == "cancelled"]
    parts = [str(o["at_ms"]) for o in commits]
    parts += [f"cancelled @{o['at_ms']}" + (" (run end)" if o["why"] == "run_end" else "")
              for o in cancels]
    if any(x["result"] == "too_late_already_committed" for x in st.cancel_attempts):
        parts.append("cancel too late")
    if st.pending_at_end:
        parts.append("still pending at run end (aborted)")
    if st.not_started:
        parts.append("not started (call id already cancelled)")
    service = "; ".join(parts) if parts else ("no" if st.tool_calls else "n/a")
    audio = a.input == "audio"
    if a.stop_after_model_speech is not None:
        stop_after = f"{a.stop_after_model_speech} after model speech"
    elif a.stop_after_request is not None:
        stop_after = f"{a.stop_after_request} after " + ("book clip end" if audio else "request")
    else:
        stop_after = a.stop_after
    if st.speech_after_call_ms is not None:
        speech = f"{st.speech_after_call_ms} (+{st.speech_after_call_ms - st.tool_call_at_ms} vs tool call)"
    else:
        speech = "none after tool call" if st.tool_call_at_ms is not None else "n/a"
    first_resp = min((r["at_ms"] for r in st.tool_responses), default=None)
    row = {
        "run": st.run,
        "input": a.input,
        "behavior": a.behavior,
        "stop_after_s": stop_after,
        "latency_s": a.latency,
        "tool_call_at_ms": ("no_tool_call" if st.tool_call_at_ms is None
                            else rel(st.tool_call_at_ms, st) if a.stop_after_request is not None
                            else st.tool_call_at_ms),
        "stop_sent_at_ms": (stop if stop is not None
                            else f"skipped ({st.stop_skipped})" if st.stop_skipped else "-"),
        "model_speech_start_ms": speech,
        "cancellation_ids_match": cancellation_ids_match(st),
        "stop_audio_end_ms": ("n/a" if not audio else st.stop_audio_end_ms
                              if st.stop_audio_end_ms is not None else "-"),
        "interrupted_seen": interrupted,
        "cancellation_seen": cancellation,
        "service_committed": service,
        "model_after_stop": said_after[:160] or "-",
        "send_method": st.send_method or "-",
        "errors": "; ".join(e[:120] for e in st.errors) or "-",
        "tool_response_sent_ms": ", ".join(str(r["at_ms"]) for r in st.tool_responses) or "no",
        "n_tool_calls": len(st.tool_calls),
        "model_before_stop": said_before[:300],
        "done_reason": st.done_reason,
        "modality": st.modality,
        "audio_chunks": st.audio_chunks_total,
    }
    if audio:
        end = st.stop_audio_end_ms
        row.update({
            "user_audio": st.utterances,
            "mic_chunks": ({"speech": st.mic.speech_chunks, "silence": st.mic.silence_chunks}
                           if st.mic else None),
            "vad_events": [dict(e, vs_stop_ms=(e["at_ms"] - stop if stop is not None else None))
                           for e in st.vad_events],
            "input_transcript": " / ".join(re.sub(r"\s+", " ", x).strip()
                                           for _, x in st.input_texts if x.strip()),
            "model_during_stop_clip": join_turns(
                [c for c in st.texts if stop is not None and stop <= c[0]
                 and (end is None or c[0] < end)])[:300],
            "model_after_stop_audio_end": join_turns(
                [c for c in st.texts if end is not None and c[0] >= end])[:300],
            "first_model_audio_ms": st.first_audio_ms,
            "tool_calls": st.tool_calls,
            "pending_at_stop": st.pending_at_stop,
            "stop_skipped": st.stop_skipped,
            "cancellations": st.cancellations,
            "cancel_attempts": st.cancel_attempts,
            "job_outcomes": st.job_outcomes,
            "tool_responses": st.tool_responses,
            "interrupted_ms": st.interrupted_ms,
            "generation_complete_ms": st.generation_complete_ms,
            "turn_complete_ms": st.turn_complete_ms,
            "model_turns": turns_with_times(st.texts),
            "input_transcripts": [{"at_ms": t, "text": x} for t, x in st.input_texts],
            "model_after_tool_response": (join_turns(
                [c for c in st.texts if c[0] >= first_resp])[:300]
                if first_resp is not None else None),
        })
    return row


COLUMNS = ["run", "behavior", "stop_after_s", "latency_s", "tool_call_at_ms",
           "stop_sent_at_ms", "interrupted_seen", "cancellation_seen",
           "service_committed", "model_after_stop", "send_method", "errors",
           "tool_response_sent_ms"]


def table_columns(args: argparse.Namespace) -> list[str]:
    cols = ["run", "input"] + COLUMNS[1:]
    if args.input == "audio":
        cols.insert(cols.index("stop_sent_at_ms") + 1, "stop_audio_end_ms")
    if args.stop_after_model_speech is not None:
        cols.insert(cols.index("tool_call_at_ms") + 1, "model_speech_start_ms")
        cols.insert(cols.index("cancellation_seen") + 1, "cancellation_ids_match")
    return cols


def append_summary(path: Path, args: argparse.Namespace, rows: list[dict],
                   modality: str) -> str:
    cols = table_columns(args)
    audio_note = ""
    if args.input == "audio":
        audio_note = (
            f", input=audio (book clip {Path(args.book_audio).name} "
            f"{pcm_seconds(args.clips['book']):.3f} s, stop clip "
            f"{pcm_seconds(args.clips['stop']):.3f} s, "
            + (f"follow-up clip {Path(args.followup_audio).name} "
               f"{pcm_seconds(args.clips['followup']):.3f} s starting "
               f"{args.followup_after_tool_call} s after the book_slot call, "
               if args.followup_audio is not None else "")
            + f"{AUDIO_MIME}, "
            f"{int(CHUNK_S * 1000)} ms chunks paced in real time, silence between utterances, "
            "automatic VAD at server default). stop_sent_at_ms is the first chunk of the "
            "stop clip, stop_audio_end_ms the last")
    if args.save_audio:
        audio_note += (", model output audio saved (--save-audio) to "
                       "results/audio_out/<scenario>_run<N>_model.wav")
    lines = [
        f"### {args.name}",
        "",
        f"model `{args.model}`, google-genai {SDK_VERSION}, "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}, behavior={args.behavior}, "
        + (f"stop_after_model_speech={args.stop_after_model_speech}s (stop clip starts "
           "that long after the first model audio chunk that follows the book_slot call, "
           "only if no tool response has been sent), "
           if args.stop_after_model_speech is not None
           else f"stop_after={args.stop_after}s, " if args.stop_after_request is None
           else f"stop_after_request={args.stop_after_request}s (after "
                + ("end of booking clip), " if args.input == "audio" else "booking request), "))
        + f"latency={args.latency}s, "
        f"honor_cancel={'yes' if args.honor_cancel else 'no'}, "
        f"scheduling={args.scheduling} (in {args.scheduling_in}), respond={args.respond}, "
        f"modality={modality}{audio_note}. Times are ms since session start.",
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for r in rows:
        lines.append("| " + " | ".join(md(r.get(c, "-")) for c in cols) + " |")
    block = "\n".join(lines) + "\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return block


# --------------------------------------------------------------- save audio --


def mime_rate(mime: str) -> int | None:
    m = re.search(r"rate=(\d+)", mime or "")
    return int(m.group(1)) if m else None


def save_run_audio(st: RunState, out_dir: Path) -> dict:
    """--save-audio: <scenario>_run<N>_model.wav (model output, chunks concatenated in
    arrival order), <scenario>_run<N>_user_<label>.wav (copies of the clips sent), and
    <scenario>_run<N>_audio.json (chunk arrival times, clip send times, key events)."""
    a = st.args
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{a.name}_run{st.run}"
    mimes = sorted({c["mime_type"] for c in st.audio_out})
    rates = {mime_rate(m) for m in mimes}
    if len(rates) == 1 and None not in rates:
        rate, rate_source = rates.pop(), "mime type"
    else:
        rate, rate_source = MODEL_AUDIO_RATE, f"default (mime types: {mimes or 'none'})"
    chunks, offset = [], 0
    for meta, data in zip(st.audio_out, st.audio_data):
        chunks.append({"t_ms": meta["t_ms"], "turn": meta["turn"], "bytes": len(data),
                       "wav_offset_ms": round(offset / 2 / rate * 1000, 1),
                       "duration_ms": round(len(data) / 2 / rate * 1000, 1)})
        offset += len(data)
    pcm = b"".join(st.audio_data)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    wav_path = out_dir / f"{stem}_model.wav"
    if pcm:
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
    user = []
    for u in st.utterances:
        src = Path(a.audio_paths[u["label"]])
        dst = out_dir / f"{stem}_user_{u['label']}.wav"
        shutil.copyfile(src, dst)
        user.append({"label": u["label"], "file": dst.name, "source": src.name,
                     "sent_start_ms": u.get("start_ms"), "sent_end_ms": u.get("end_ms"),
                     "duration_ms": round(pcm_seconds(load_pcm(src)) * 1000, 1),
                     "sample_rate": AUDIO_RATE})
    sidecar = {
        "scenario": a.name, "run": st.run, "wall": st.wall, "model": a.model,
        "time_base": "ms since session start (time.monotonic() when the run started). "
                     "t_ms of a model chunk is when the harness received it; "
                     "sent_start_ms / sent_end_ms of a user clip are when its first / last "
                     "100 ms chunk was sent.",
        "model_audio": {
            "file": wav_path.name if pcm else None,
            "mime_types": mimes, "sample_rate": rate, "sample_rate_from": rate_source,
            "channels": 1, "sample_width_bits": 16,
            "duration_s": round(len(pcm) / 2 / rate, 3), "chunks": len(chunks),
            "first_chunk_ms": chunks[0]["t_ms"] if chunks else None,
            "last_chunk_ms": chunks[-1]["t_ms"] if chunks else None,
            "note": "Chunks are concatenated in arrival order with no gaps. The server "
                    "sends audio faster than real time, so wav_offset_ms is not the "
                    "arrival time: place each chunk at its t_ms or later. A client that "
                    "plays audio drops what is still queued when `interrupted` arrives; "
                    "this file keeps everything received.",
        },
        "chunks": chunks,
        "user_clips": user,
        "events": {
            "tool_calls": st.tool_calls,
            "tool_responses": st.tool_responses,
            "service": st.job_outcomes,
            "cancellations": st.cancellations,
            "interrupted_ms": st.interrupted_ms,
            "generation_complete_ms": st.generation_complete_ms,
            "turn_complete_ms": st.turn_complete_ms,
            "voice_activity": st.vad_events,
            "stop_sent_at_ms": st.stop_sent_at_ms,
            "stop_audio_end_ms": st.stop_audio_end_ms,
            "model_transcript": [{"t_ms": t, "turn": turn, "text": x} for t, turn, x in st.texts],
            "input_transcript": [{"t_ms": t, "text": x} for t, x in st.input_texts],
        },
    }
    side_path = out_dir / f"{stem}_audio.json"
    side_path.write_text(redact(json.dumps(sidecar, indent=1, ensure_ascii=False, default=str)),
                         encoding="utf-8")
    return {"model_wav": wav_path.name if pcm else None, "sidecar": side_path.name,
            "model_audio_s": sidecar["model_audio"]["duration_s"], "sample_rate": rate,
            "user_clips": [u["file"] for u in user]}


# -------------------------------------------------------------------- main --


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Gemini Live API: in-flight tool call vs. user 'stop' (see README.md).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--name", default=None,
                   help="scenario name; results go to results/<name>.jsonl")
    p.add_argument("-n", "--runs", type=int, default=3, help="number of sessions to run")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--behavior", choices=["unset", "BLOCKING", "NON_BLOCKING"],
                   default="unset", help="FunctionDeclaration.behavior")
    p.add_argument("--input", choices=["text", "audio"], default="text",
                   help="text: send_realtime_input(text=...); audio: stream --book-audio and "
                        "--stop-audio as 16 kHz PCM in 100 ms chunks paced in real time, "
                        "silence between utterances, automatic VAD at server default")
    p.add_argument("--book-audio", default=str(AUDIO_DIR / "book.wav"),
                   help="16 kHz 16-bit mono WAV for the booking request (--input audio)")
    p.add_argument("--stop-audio", default=str(AUDIO_DIR / "stop.wav"),
                   help="16 kHz 16-bit mono WAV for the stop (--input audio)")
    p.add_argument("--stop-after", type=float, default=1.0,
                   help="seconds after the tool call arrives to send the stop message "
                        "(audio: to the first chunk of the stop clip)")
    p.add_argument("--stop-after-model-speech", type=float, default=None,
                   help="scenario G (--input audio): start the stop clip this many seconds "
                        "after the first model audio chunk that arrives after the book_slot "
                        "call, and only if no tool response has been sent yet (overrides "
                        "--stop-after)")
    p.add_argument("--followup-audio", default=None,
                   help="scenario G2 (--input audio): 16 kHz 16-bit mono WAV streamed as a "
                        "second user utterance --followup-after-tool-call s after the "
                        "book_slot call arrives")
    p.add_argument("--followup-after-tool-call", type=float, default=0.5)
    p.add_argument("--save-audio", action="store_true",
                   help="write the model's output audio per run to results/audio_out/"
                        "<name>_run<N>_model.wav plus a JSON sidecar with chunk arrival "
                        "times, and copies of the user clips with their send times")
    p.add_argument("--stop-after-request", type=float, default=None,
                   help="send the stop this many seconds after the booking request instead "
                        "(timer starts at the request, not at the tool call; audio: at the "
                        "last chunk of the booking clip; overrides --stop-after; no "
                        "send_client_content fallback)")
    p.add_argument("--min-post-stop", type=float, default=0.0,
                   help="keep listening at least this many seconds after the stop "
                        "(audio: after the end of the stop clip; same for --post-stop-window)")
    p.add_argument("--latency", type=float, default=4.0,
                   help="seconds the fake booking service takes to commit")
    p.add_argument("--respond", choices=["immediate", "never"], default="immediate",
                   help="immediate: send the tool response as soon as the job commits")
    p.add_argument("--honor-cancel", action="store_true",
                   help="cancel the booking job when toolCallCancellation names its call id")
    p.add_argument("--scheduling", choices=["none", "INTERRUPT", "WHEN_IDLE", "SILENT"],
                   default="none", help="scheduling for the tool response (NON_BLOCKING only)")
    p.add_argument("--scheduling-in", choices=["field", "response", "both"], default="both",
                   help="where to put scheduling: FunctionResponse.scheduling field, "
                        "inside the response dict (docs example), or both")
    p.add_argument("--send-method", choices=["auto", "realtime", "client_content"],
                   default="auto", help="auto: send_realtime_input(text=...), fall back "
                                        "to send_client_content if no tool call")
    p.add_argument("--modality", choices=["auto", "TEXT", "AUDIO"], default="auto",
                   help="auto: try TEXT, fall back to AUDIO + output transcription")
    p.add_argument("--tool-call-wait", type=float, default=15.0)
    p.add_argument("--post-stop-window", type=float, default=12.0)
    p.add_argument("--quiet", type=float, default=3.0,
                   help="seconds of silence after a tool response before a run can end")
    p.add_argument("--run-timeout", type=float, default=45.0)
    p.add_argument("--between-runs", type=float, default=2.0)
    p.add_argument("--results-dir", default=str(HERE / "results"))
    p.add_argument("-v", "--verbose", action="store_true", help="echo events to stdout")
    args = p.parse_args(argv)
    if args.followup_audio is not None and args.input != "audio":
        p.error("--followup-audio needs --input audio")
    if args.stop_after_model_speech is not None:
        if args.input != "audio":
            p.error("--stop-after-model-speech needs --input audio")
        if args.stop_after_request is not None:
            p.error("--stop-after-model-speech and --stop-after-request are exclusive")
    if args.name is None:
        stop = (f"speechstop{args.stop_after_model_speech}"
                if args.stop_after_model_speech is not None
                else f"stop{args.stop_after}" if args.stop_after_request is None
                else f"stopreq{args.stop_after_request}")
        args.name = (f"{args.behavior.lower()}_{stop}_lat{args.latency}"
                     f"{'_honor' if args.honor_cancel else ''}"
                     f"{'' if args.scheduling == 'none' else '_' + args.scheduling.lower()}"
                     f"{'_audio' if args.input == 'audio' else ''}")
    args.clips = {}
    args.audio_paths = {"book_request": args.book_audio, "stop": args.stop_audio}
    if args.followup_audio is not None:
        args.audio_paths["followup"] = args.followup_audio
    return args


async def amain(args: argparse.Namespace, connect: Callable[..., Any] | None = None) -> int:
    if args.input == "audio" and not args.clips:
        try:
            args.clips = {"book": load_pcm(Path(args.book_audio)),
                          "stop": load_pcm(Path(args.stop_audio))}
            if args.followup_audio is not None:
                args.clips["followup"] = load_pcm(Path(args.followup_audio))
        except Exception as exc:
            print(f"cannot load audio clips: {exc}", file=sys.stderr)
            return 2
    if connect is None:
        load_dotenv(HERE / ".env")
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            print("GEMINI_API_KEY is not set (put it in .env next to this script).",
                  file=sys.stderr)
            return 2
        _SECRETS.append(key)
        client = genai.Client(api_key=key, vertexai=False)
        connect = client.aio.live.connect
    results = Path(args.results_dir)
    out = JsonlWriter(results / f"{args.name}.jsonl", args.name, args.verbose)
    modality = "TEXT" if args.modality == "auto" else args.modality
    rows = []
    try:
        for run in range(1, args.runs + 1):
            try:
                st = await run_once(args, run, modality, out, connect)
                if args.modality == "auto" and looks_like_modality_rejection(st):
                    out.write(run, st.clock.ms(), "modality_rejected", tried="TEXT",
                              errors=st.errors, retry_with="AUDIO")
                    print(f"[{args.name} run {run}] TEXT modality rejected, retrying with AUDIO")
                    modality = "AUDIO"
                    await asyncio.sleep(args.between_runs)
                    st = await run_once(args, run, modality, out, connect)
                row = summarize(st)
                if args.save_audio:
                    try:
                        row["saved_audio"] = save_run_audio(st, results / "audio_out")
                    except Exception as exc:
                        row["saved_audio"] = {"error": err_text(exc)}
                        out.write(run, -1, "save_audio_failed", detail=err_text(exc))
                quota = quota_error(st)
            except Exception as exc:  # never let one run kill the matrix
                quota = err_text(exc) if QUOTA_RE.search(err_text(exc)) else None
                out.write(run, -1, "run_crashed", detail=err_text(exc))
                row = {c: "-" for c in table_columns(args)} | {"run": run,
                                                               "errors": err_text(exc)}
                row.update(behavior=args.behavior, stop_after_s=args.stop_after,
                           latency_s=args.latency, input=args.input)
            out.write(run, -1, "run_end", summary=row)
            rows.append(row)
            print(redact(f"[{args.name} run {run}/{args.runs}] tool_call={row['tool_call_at_ms']} "
                         f"stop={row['stop_sent_at_ms']}"
                         + (f"..{row['stop_audio_end_ms']}" if args.input == "audio" else "")
                         + f" interrupted={row['interrupted_seen']} "
                         f"cancellation={row['cancellation_seen']} "
                         f"service={row['service_committed']} "
                         f"said_after_stop=\"{row['model_after_stop']}\" "
                         f"errors={row['errors']}"))
            if quota:
                out.write(run, -1, "stopped_on_quota_or_billing_error", detail=quota)
                print(redact(f"[{args.name}] stopping: quota/billing error: {quota}"))
                break
            if run < args.runs:
                await asyncio.sleep(args.between_runs)
    finally:
        out.close()
    block = append_summary(results / "summary.md", args, rows, modality)
    print()
    print(redact(block))
    return EXIT_QUOTA if rows and quota else 0


def main() -> None:
    args = parse_args()
    try:
        sys.exit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
