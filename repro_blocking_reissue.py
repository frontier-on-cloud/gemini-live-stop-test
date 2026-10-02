"""Gemini Live API: after "stop", a BLOCKING function call is re-issued with a new id and no
toolCallCancellation arrives. book_slot is declared with behavior=BLOCKING; the user types a
booking request, the model emits a toolCall, and 1 s later the user types "Actually, stop.
Don't book it." Seen in 6 text runs: serverContent.interrupted 14-21 ms after the stop and no
toolCallCancellation (6 of 6), then 0.47-0.82 s after the stop a second toolCall for the same
function and slot, new id (5 of 6). Each call gets {"status": "booked"} 4 s after it arrives.
Run: GEMINI_API_KEY=... uv run repro_blocking_reissue.py [--runs N] [--behavior B], B one of
BLOCKING (default), NON_BLOCKING, UNSET; the key can also be in .env here. gemini-3.8-live,
Google AI Studio endpoint, google-genai 2.25.0, AUDIO responses with output transcription
(this model rejects TEXT), audio bytes discarded. Written 2026-10-02; runs in README.md."""
import argparse, asyncio, os, sys, time

from dotenv import load_dotenv
from google import genai
from google.genai import types

MODEL = "gemini-3.8-live"
SYSTEM = ("You are a scheduling assistant. When the user asks to book a slot, call book_slot "
          "immediately, then tell the user you are booking it. Keep replies to one or two short "
          "sentences. If the user tells you to stop or cancel, tell them plainly whether the "
          "booking was already made or not.")
BOOK, STOP = "Book me the 3pm slot tomorrow, please.", "Actually, stop. Don't book it."


def config(behavior: str) -> types.LiveConnectConfig:
    slot = types.Schema(type="STRING", description="The slot to book, e.g. 'tomorrow 3pm'.")
    params = types.Schema(type="OBJECT", properties={"slot": slot}, required=["slot"])
    decl = types.FunctionDeclaration(
        name="book_slot", parameters=params,
        description="Book an appointment slot for the user. Returns a confirmation.",
        **({} if behavior == "UNSET" else {"behavior": types.Behavior(behavior)}))
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM)]),
        tools=[types.Tool(function_declarations=[decl])])


async def run(client: genai.Client, behavior: str) -> None:
    t0, stop_ms, calls, cancels, words, tasks = time.monotonic(), None, [], [], [], []
    first_call = asyncio.Event()
    ms = lambda: int((time.monotonic() - t0) * 1000)

    def log(text: str) -> int:
        now = ms()
        print(f"{now:>6} ms{'' if stop_ms is None else f' (stop+{now - stop_ms})'}  {text}")
        return now

    def flush_transcript() -> None:  # one line per turn, stamped with its first chunk
        if words:
            log(f'model said (from {words[0][0]} ms): "{"".join(w for _, w in words).strip()}"')
            words.clear()

    async with client.aio.live.connect(model=MODEL, config=config(behavior)) as session:
        async def answer(fc: types.FunctionCall) -> None:  # a slow backend
            await asyncio.sleep(4)
            await session.send_tool_response(function_responses=[types.FunctionResponse(
                id=fc.id, name=fc.name, response={"status": "booked"})])
            log(f"sent tool response for {fc.id}")

        async def receive() -> None:
            try:
                while True:  # session.receive() returns after each turnComplete
                    async for msg in session.receive():
                        sc = msg.server_content
                        if sc and sc.output_transcription and sc.output_transcription.text:
                            words.append((ms(), sc.output_transcription.text))
                        if sc and sc.interrupted:
                            log("interrupted")
                        for fc in (msg.tool_call.function_calls or []) if msg.tool_call else []:
                            calls.append(fc.id)
                            log(f"toolCall {fc.name} id={fc.id} args={dict(fc.args or {})}")
                            tasks.append(asyncio.create_task(answer(fc)))
                            first_call.set()
                        if msg.tool_call_cancellation:
                            cancels.extend(msg.tool_call_cancellation.ids or [])
                            log(f"toolCallCancellation ids={msg.tool_call_cancellation.ids}")
                        if sc and sc.turn_complete:
                            flush_transcript()
                            log("turnComplete")
            except Exception as exc:  # e.g. the server closed the websocket
                log(f"session ended: {type(exc).__name__}: {exc}")

        receiver = asyncio.create_task(receive())
        await session.send_realtime_input(text=BOOK)
        log(f"sent request: {BOOK!r}")
        got_call = asyncio.create_task(first_call.wait())
        await asyncio.wait([receiver, got_call], timeout=15, return_when=asyncio.FIRST_COMPLETED)
        if first_call.is_set() and not receiver.done():
            await asyncio.sleep(1)
            await session.send_realtime_input(text=STOP)
            stop_ms = log(f"sent stop: {STOP!r}")
            await asyncio.wait([receiver], timeout=15)  # listen 15 s past the stop, then exit
        flush_transcript()
        for t in (receiver, got_call, *tasks):
            t.cancel()
        await asyncio.gather(receiver, got_call, *tasks, return_exceptions=True)
    print(f"summary: {len(calls)} toolCall(s) for one booking request, behavior={behavior}\n"
          f"toolCall ids: {', '.join(calls) or 'none'}\n"
          f"toolCallCancellation: {('ids ' + ', '.join(cancels)) if cancels else 'none received'}")


async def main() -> None:
    p = argparse.ArgumentParser(description="Gemini Live: BLOCKING call re-issued after stop.")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--behavior", choices=["BLOCKING", "NON_BLOCKING", "UNSET"], default="BLOCKING")
    a = p.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or sys.exit("no GEMINI_API_KEY"))
    for i in range(1, a.runs + 1):
        print(f"run {i}/{a.runs}  model={MODEL}  google-genai {genai.__version__}  "
              f"behavior={a.behavior}  {time.strftime('%Y-%m-%d %H:%M:%S')}")
        await run(client, a.behavior)
        await asyncio.sleep(2 if i < a.runs else 0)


if __name__ == "__main__":
    asyncio.run(main())
