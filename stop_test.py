"""Gemini Live API: what happens to an in-flight tool call when the user says "stop"?

One run = one Live session:
  1. send "Book me the 3pm slot tomorrow, please."
  2. on the book_slot tool call, start a fake booking job that commits after --latency s
  3. --stop-after s after the tool call arrived, send "Actually, stop. Don't book it."
  4. record every server event (interrupted, turn_complete, generation_complete,
     tool_call_cancellation, model text/transcript, audio chunks) with a timestamp
     in ms relative to session start (time.monotonic()).

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
import sys
import time
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

# Raw (camelCase) keys this script knows about. Anything else is logged by name.
KNOWN_RAW_TOP = {
    "setupComplete", "serverContent", "toolCall", "toolCallCancellation",
    "goAway", "sessionResumptionUpdate", "usageMetadata", "voiceActivity",
    "voiceActivityDetectionSignal",
}
KNOWN_RAW_SERVER_CONTENT = {
    "modelTurn", "turnComplete", "generationComplete", "interrupted",
    "outputTranscription", "inputTranscription", "turnCompleteReason",
    "waitingForInput", "interactionStatus",
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
    stop_sent_at_ms: int | None = None
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
    def audio_chunk(self, nbytes: int) -> None:
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

    def flush_audio(self, reason: str) -> None:
        if self.audio_seg is not None:
            self.emit("audio_segment", closed_by=reason, **self.audio_seg)
            self.audio_seg = None

    def settled(self, now: int) -> bool:
        """Stop condition after the stop message (see README)."""
        if self.stop_sent_at_ms is None:
            return False
        if now - self.stop_sent_at_ms < self.args.min_post_stop * 1000:
            return False
        if any(not t.done() for t in self.service.jobs.values()):
            return False
        # The model must have replied to the stop. A turn_complete that only closes
        # the interrupted turn (no output after the stop) does not count.
        if self.last_output_ms < self.stop_sent_at_ms:
            return False
        last_tc = max(self.turn_complete_ms, default=-1)
        if last_tc < self.stop_sent_at_ms:
            return False
        if self.last_output_ms > last_tc:  # model is mid-turn
            return False
        last_resp = max((r["at_ms"] for r in self.tool_responses), default=-1)
        if last_resp > last_tc:  # give the model a chance to react to the response
            return now - max(last_resp, self.last_output_ms) >= self.args.quiet * 1000
        return True


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
                st.audio_chunk(len(part.inline_data.data or b""))
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
        st.emit("input_transcript", text=sc.input_transcription.text)
    if sc.interrupted:
        st.flush_audio("interrupted")
        t = st.emit("interrupted", **extra)
        st.interrupted_ms.append(t)
        st.turn_idx += 1
    if sc.generation_complete:
        st.flush_audio("generation_complete")
        st.emit("generation_complete", **extra)
    if sc.turn_complete:
        st.flush_audio("turn_complete")
        t = st.emit("turn_complete", **extra)
        st.turn_complete_ms.append(t)
        st.turn_idx += 1
    elif extra and not (sc.interrupted or sc.generation_complete):
        st.emit("server_content_status", **extra)
    handled = {"model_turn", "output_transcription", "input_transcription", "interrupted",
               "generation_complete", "turn_complete", "turn_complete_reason",
               "waiting_for_input", "interaction_status"}
    other = sorted(k for k, v in sc if v is not None and k not in handled)
    if other:
        st.emit("unhandled_server_content", fields=other)


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
                if msg.voice_activity or msg.voice_activity_detection_signal:
                    st.emit("voice_activity")
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


async def user_script(session: Any, st: RunState) -> None:
    a = st.args
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
        if st.stop_sent_at_ms is not None:
            now = st.clock.ms()
            if now - st.stop_sent_at_ms >= st.args.post_stop_window * 1000:
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
    st.emit("run_start", wall=datetime.now().isoformat(timespec="seconds"),
            model=args.model, sdk=SDK_VERSION, behavior=args.behavior,
            stop_after_s=args.stop_after, stop_after_request_s=args.stop_after_request,
            min_post_stop_s=args.min_post_stop, latency_s=args.latency,
            honor_cancel=args.honor_cancel, scheduling=args.scheduling,
            scheduling_in=args.scheduling_in, respond=args.respond,
            modality=modality, send_method_arg=args.send_method)
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
    return {
        "run": st.run,
        "behavior": a.behavior,
        "stop_after_s": (a.stop_after if a.stop_after_request is None
                         else f"{a.stop_after_request} after request"),
        "latency_s": a.latency,
        "tool_call_at_ms": ("no_tool_call" if st.tool_call_at_ms is None
                            else rel(st.tool_call_at_ms, st) if a.stop_after_request is not None
                            else st.tool_call_at_ms),
        "stop_sent_at_ms": stop if stop is not None else "-",
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


COLUMNS = ["run", "behavior", "stop_after_s", "latency_s", "tool_call_at_ms",
           "stop_sent_at_ms", "interrupted_seen", "cancellation_seen",
           "service_committed", "model_after_stop", "send_method", "errors",
           "tool_response_sent_ms"]


def append_summary(path: Path, args: argparse.Namespace, rows: list[dict],
                   modality: str) -> str:
    lines = [
        f"### {args.name}",
        "",
        f"model `{args.model}`, google-genai {SDK_VERSION}, "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}, behavior={args.behavior}, "
        + (f"stop_after={args.stop_after}s, " if args.stop_after_request is None
           else f"stop_after_request={args.stop_after_request}s (after booking request), ")
        + f"latency={args.latency}s, "
        f"honor_cancel={'yes' if args.honor_cancel else 'no'}, "
        f"scheduling={args.scheduling} (in {args.scheduling_in}), respond={args.respond}, "
        f"modality={modality}. Times are ms since session start.",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    for r in rows:
        lines.append("| " + " | ".join(md(r[c]) for c in COLUMNS) + " |")
    block = "\n".join(lines) + "\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return block


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
    p.add_argument("--stop-after", type=float, default=1.0,
                   help="seconds after the tool call arrives to send the stop message")
    p.add_argument("--stop-after-request", type=float, default=None,
                   help="send the stop this many seconds after the booking request instead "
                        "(timer starts at the request, not at the tool call; overrides "
                        "--stop-after; no send_client_content fallback)")
    p.add_argument("--min-post-stop", type=float, default=0.0,
                   help="keep listening at least this many seconds after the stop")
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
    if args.name is None:
        stop = (f"stop{args.stop_after}" if args.stop_after_request is None
                else f"stopreq{args.stop_after_request}")
        args.name = (f"{args.behavior.lower()}_{stop}_lat{args.latency}"
                     f"{'_honor' if args.honor_cancel else ''}"
                     f"{'' if args.scheduling == 'none' else '_' + args.scheduling.lower()}")
    return args


async def amain(args: argparse.Namespace, connect: Callable[..., Any] | None = None) -> int:
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
                quota = quota_error(st)
            except Exception as exc:  # never let one run kill the matrix
                quota = err_text(exc) if QUOTA_RE.search(err_text(exc)) else None
                out.write(run, -1, "run_crashed", detail=err_text(exc))
                row = {c: "-" for c in COLUMNS} | {"run": run, "errors": err_text(exc)}
                row.update(behavior=args.behavior, stop_after_s=args.stop_after,
                           latency_s=args.latency)
            out.write(run, -1, "run_end", summary=row)
            rows.append(row)
            print(redact(f"[{args.name} run {run}/{args.runs}] tool_call={row['tool_call_at_ms']} "
                         f"stop={row['stop_sent_at_ms']} interrupted={row['interrupted_seen']} "
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
