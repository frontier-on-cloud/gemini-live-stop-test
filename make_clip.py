#!/usr/bin/env python3
"""Render a short demo clip of one measured run: BLOCKING tool call, user says stop.

Data: results/audio_C_rerun_blocking_stop1.0_honor.jsonl (run 1) and the files
that --save-audio wrote for that run in results/audio_out/ (model WAV + JSON
sidecar). The user clips are assets/audio/book.wav and assets/audio/stop.wav.
Every time shown in the clip is read from those files (ms since session
start); nothing is sped up.

Output:
  results/clip/stop-test-C1.mp4  1280x720, 30 fps, H.264 + AAC (mono, 24 kHz)
  results/clip/stop-test-C1.gif  800 px wide, 12 fps, no audio

Run (Homebrew ffmpeg is not needed; imageio-ffmpeg ships a static ffmpeg):
  uv run --with imageio-ffmpeg --with pillow --with numpy python make_clip.py
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
SCENARIO = "audio_C_rerun_blocking_stop1.0_honor"
RUN = 1
JSONL = ROOT / "results" / f"{SCENARIO}.jsonl"
AUDIO_DIR = ROOT / "results" / "audio_out"
SIDECAR = AUDIO_DIR / f"{SCENARIO}_run{RUN}_audio.json"
MODEL_WAV = AUDIO_DIR / f"{SCENARIO}_run{RUN}_model.wav"
USER_CLIPS = {
    "book_request": ROOT / "assets" / "audio" / "book.wav",
    "stop": ROOT / "assets" / "audio" / "stop.wav",
}
# What the clips say (README, "Audio input"). The server's own transcript of
# the first one is "Book me the 3:00 p.m. slot tomorrow, please."
USER_TEXT = {
    "book_request": "Book me the 3pm slot tomorrow, please.",
    "stop": "Actually, stop. Don't book it.",
}
OUT_DIR = ROOT / "results" / "clip"
OUT_MP4 = OUT_DIR / "stop-test-C1.mp4"
OUT_GIF = OUT_DIR / "stop-test-C1.gif"

TITLE = "Gemini 3.8 Live, BLOCKING tool call: the user says stop"
W, H = 1280, 720
FPS = 30
SS = 2  # supersampling factor for smooth shapes
SR = 24000  # audio track sample rate
TAIL_MS = 1500  # timeline runs this long past the end of the model's audio
HOLD_S = 1.0  # then the last frame holds (clock stopped) so the end can be read
GIF_FPS = 12
GIF_W = 800

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# ---------------------------------------------------------------- colors
# Light surface, dark ink, two hues plus greys (dataviz reference palette:
# blue slot 1, green slot 6; the pair passes the CVD and contrast checks).
BG = (246, 245, 242)
PANEL = (255, 255, 255)
BORDER = (220, 218, 212)
INK = (26, 26, 25)
INK_2 = (82, 81, 78)
INK_3 = (130, 129, 124)
TRACK = (232, 230, 225)
USER_FILL = (236, 234, 230)
BLUE = (42, 120, 214)  # model / server events
BLUE_TINT = (226, 237, 251)
GREEN = (0, 131, 0)  # booking committed (white text on it: 4.9:1)
WHITE = (255, 255, 255)


# ---------------------------------------------------------------- data
def load_run() -> dict:
    events = [json.loads(line) for line in JSONL.read_text().splitlines() if line.strip()]
    events = [e for e in events if e.get("run") == RUN]
    if not events:
        raise SystemExit(f"run {RUN} not found in {JSONL}")

    def one(name: str, **match):
        hits = [e for e in events if e["event"] == name and all(e.get(k) == v for k, v in match.items())]
        if not hits:
            raise SystemExit(f"no {name} {match} in run {RUN}")
        return hits[0]

    side = json.loads(SIDECAR.read_text())
    tool_call = one("tool_call_received")
    job = one("service_job_started")
    committed = one("service_committed")
    response = one("tool_response_sent")
    chunks = [e for e in events if e["event"] == "model_transcript"]
    cancellations = [e for e in events if e["event"] == "tool_call_cancellation"]
    clips = {c["label"]: c for c in side["user_clips"]}
    d = {
        "book_ms": one("user_audio_start", label="book_request")["t_ms"],
        "stop_ms": one("user_audio_start", label="stop")["t_ms"],
        "tool_call_ms": tool_call["t_ms"],
        "call_id": tool_call["call_id"],
        "latency_ms": int(round(job["latency_s"] * 1000)),
        "job_start_ms": job["t_ms"],
        "interrupted_ms": one("interrupted")["t_ms"],
        "committed_ms": committed["t_ms"],
        "slot": committed["slot"],
        "response_ms": response["t_ms"],
        "response": response["response"],
        "model_text_ms": chunks[0]["t_ms"],
        "model_text": "".join(c["text"] for c in chunks).strip(),
        "session_closed_ms": one("session_closed")["t_ms"],
        "n_cancellations": len(cancellations) + len(side["events"].get("cancellations") or []),
        "model_first_chunk_ms": side["model_audio"]["first_chunk_ms"],
        "model_sr": side["model_audio"]["sample_rate"],
    }
    # The sidecar and the JSONL must agree on when the clips were sent.
    assert clips["book_request"]["sent_start_ms"] == d["book_ms"], "book clip offset mismatch"
    assert clips["stop"]["sent_start_ms"] == d["stop_ms"], "stop clip offset mismatch"
    return d


# ---------------------------------------------------------------- audio
def decode(path: Path) -> np.ndarray:
    """Decode a WAV to mono float32 at SR with the static ffmpeg (resampling if needed)."""
    cmd = [FFMPEG, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
           "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def build_mix(d: dict, total_s: float, path: Path) -> float:
    n = int(round(total_s * SR))
    mix = np.zeros(n, dtype=np.float32)

    def place(x: np.ndarray, at_ms: float) -> None:
        i = int(round(at_ms * SR / 1000))
        seg = x[: max(0, n - i)]
        mix[i : i + len(seg)] += seg

    place(decode(USER_CLIPS["book_request"]), d["book_ms"])
    place(decode(USER_CLIPS["stop"]), d["stop_ms"])
    place(decode(MODEL_WAV), d["model_first_chunk_ms"])
    peak = float(np.abs(mix).max())
    mix *= 0.89 / peak  # -1 dBFS peak, nothing clips
    pcm = np.clip(np.round(mix * 32767), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return peak


# ---------------------------------------------------------------- drawing
FONT_CANDIDATES = {
    "regular": [("/System/Library/Fonts/HelveticaNeue.ttc", 0), ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
                ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0)],
    "bold": [("/System/Library/Fonts/HelveticaNeue.ttc", 1), ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
             ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0)],
    "mono": [("/System/Library/Fonts/Menlo.ttc", 0), ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0)],
    "mono_bold": [("/System/Library/Fonts/Menlo.ttc", 1), ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0)],
}
_font_cache: dict = {}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key not in _font_cache:
        for path, index in FONT_CANDIDATES[kind]:
            if Path(path).exists():
                _font_cache[key] = ImageFont.truetype(path, size * SS, index=index)
                break
        else:
            raise SystemExit(f"no {kind} font found")
    return _font_cache[key]


def tw(s: str, f) -> float:
    """Text width in 1x pixels."""
    return f.getlength(s) / SS


def wrap(s: str, f, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and tw(trial, f) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur] if cur else lines


class Canvas:
    def __init__(self) -> None:
        self.im = Image.new("RGB", (W * SS, H * SS), BG)
        self.d = ImageDraw.Draw(self.im)

    @staticmethod
    def _s(v):
        return [round(x * SS) for x in v]

    def rrect(self, box, r, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(self._s(box), radius=r * SS, fill=fill, outline=outline,
                                 width=round(width * SS) if outline else 0)

    def line(self, pts, fill, width=1):
        self.d.line(self._s([c for p in pts for c in p]), fill=fill, width=round(width * SS))

    def dot(self, x, y, r, fill, ring=None):
        if ring:
            self.d.ellipse(self._s((x - r - 2, y - r - 2, x + r + 2, y + r + 2)), fill=ring)
        self.d.ellipse(self._s((x - r, y - r, x + r, y + r)), fill=fill)

    def ring(self, x, y, r, color, width=2):
        self.d.ellipse(self._s((x - r, y - r, x + r, y + r)), outline=color, width=round(width * SS))

    def poly(self, pts, fill):
        self.d.polygon(self._s([c for p in pts for c in p]), fill=fill)

    def text(self, x, y, s, f, fill, anchor="la"):
        self.d.text((x * SS, y * SS), s, font=f, fill=fill, anchor=anchor)

    def frame(self) -> Image.Image:
        return self.im.resize((W, H), Image.LANCZOS)


def secs(ms: float) -> str:
    return f"{ms / 1000:.2f} s"


# layout
M = 24
HEAD_H = 76
PANEL_TOP, PANEL_BOT = 86, 500
LEFT = (M, PANEL_TOP, 648, PANEL_BOT)
RIGHT = (664, PANEL_TOP, W - M, PANEL_BOT)
STRIP = (M, 512, W - M, H - 10)
AX_X0, AX_X1, AX_Y = 64, 1216, 652
ROW_Y = {1: 552, 2: 582, 3: 612}  # label rows in the event strip (text top)


def draw_header(c: Canvas, t_ms: float) -> None:
    c.text(M + 4, 16, TITLE, font("bold", 30), INK)
    c.text(M + 4, 52, f"Measured run: scenario audio_C re-run, run {RUN}. Real time, session clock.",
           font("regular", 18), INK_2)
    clock = f"{t_ms / 1000:.2f} s"
    fc = font("mono_bold", 34)
    c.text(W - M - 4, 14, clock, fc, INK, anchor="ra")
    c.text(W - M - 4 - tw(clock, fc) - 10, 26, "t =", font("regular", 22), INK_2, anchor="ra")


def draw_panel(c: Canvas, box, title: str, note: str = "") -> None:
    c.rrect(box, 12, fill=PANEL, outline=BORDER, width=1)
    c.text(box[0] + 20, box[1] + 16, title, font("bold", 24), INK)
    if note:
        c.text(box[2] - 20, box[1] + 20, note, font("regular", 18), INK_2, anchor="ra")


def draw_conversation(c: Canvas, d: dict, t_ms: float) -> None:
    draw_panel(c, LEFT, "Conversation", "captions at send / transcript time")
    x0, _, x1, _ = LEFT
    f_body, f_role = font("regular", 26), font("regular", 19)
    pad_x, pad_y, line_h = 18, 13, 33
    max_text = 500
    msgs = [
        ("user", d["book_ms"], USER_TEXT["book_request"]),
        ("user", d["stop_ms"], USER_TEXT["stop"]),
        ("model", d["model_text_ms"], d["model_text"]),
    ]
    y = LEFT[1] + 62
    for who, at, text in msgs:
        lines = wrap(text, f_body, max_text)
        bw = max(tw(s, f_body) for s in lines) + 2 * pad_x
        bh = len(lines) * line_h + 2 * pad_y - 4
        block_h = 26 + bh + 16
        if t_ms >= at:
            if who == "user":
                bx1 = x1 - 20
                bx0 = bx1 - bw
                c.text(bx1, y, f"User, {secs(at)}", f_role, INK_2, anchor="ra")
                c.rrect((bx0, y + 26, bx1, y + 26 + bh), 16, fill=USER_FILL)
            else:
                bx0 = x0 + 20
                bx1 = bx0 + bw
                c.dot(bx0 + 6, y + 11, 5, BLUE)
                c.text(bx0 + 18, y, f"Model (spoken reply), {secs(at)}", f_role, INK_2)
                c.rrect((bx0, y + 26, bx1, y + 26 + bh), 16, fill=BLUE_TINT, outline=BLUE, width=2)
            for i, s in enumerate(lines):
                c.text(bx0 + pad_x, y + 26 + pad_y + i * line_h, s, f_body, INK)
        y += block_h


def draw_service(c: Canvas, d: dict, t_ms: float) -> None:
    draw_panel(c, RIGHT, "Booking service", f"fake service, {d['latency_ms'] / 1000:.1f} s latency")
    x0, y0, x1, _ = RIGHT
    ix0, ix1 = x0 + 20, x1 - 20
    f_time = font("regular", 19)
    if t_ms < d["job_start_ms"]:
        c.text(ix0, y0 + 70, "idle, no tool call yet", font("regular", 24), INK_3)
        return
    # job started
    y = y0 + 64
    c.text(ix0, y, "job started", font("bold", 26), INK)
    c.text(ix1, y + 4, secs(d["job_start_ms"]), f_time, INK_2, anchor="ra")
    c.text(ix0, y + 36, f"(call id {d['call_id']})", font("mono", 20), INK_2)
    # progress bar over the configured latency
    by = y + 78
    frac = min(1.0, max(0.0, (t_ms - d["job_start_ms"]) / d["latency_ms"]))
    c.rrect((ix0, by, ix1, by + 22), 11, fill=TRACK)
    if frac > 0:
        done = t_ms >= d["committed_ms"]
        c.rrect((ix0, by, ix0 + max(22, (ix1 - ix0) * frac), by + 22), 11, fill=GREEN if done else INK_2)
    if t_ms < d["committed_ms"]:
        status = f"latency {d['latency_ms'] / 1000:.1f} s, elapsed {(t_ms - d['job_start_ms']) / 1000:.2f} s"
    else:
        status = f"latency {d['latency_ms'] / 1000:.1f} s, committed at {secs(d['committed_ms'])}"
    c.text(ix0, by + 32, status, font("regular", 20), INK_2)
    # committed
    cy = by + 76
    if t_ms >= d["committed_ms"]:
        r = d["response"]
        c.rrect((ix0, cy, ix1, cy + 62), 10, fill=GREEN)
        # check mark (drawn, not a glyph)
        c.line([(ix0 + 20, cy + 32), (ix0 + 29, cy + 41), (ix0 + 45, cy + 22)], WHITE, width=4)
        label = f"COMMITTED, {r.get('confirmation_id', '?')}, {d['slot']}"
        size = 28
        while size > 18 and tw(label, font("bold", size)) > ix1 - ix0 - 60 - 16:
            size -= 1
        c.text(ix0 + 60, cy + 31, label, font("bold", size), WHITE, anchor="lm")
    # tool response
    ry = cy + 86
    if t_ms >= d["response_ms"]:
        c.text(ix0, ry, f"tool response sent: status {d['response'].get('status')}", font("regular", 25), INK)
        c.text(ix1, ry + 4, secs(d["response_ms"]), f_time, INK_2, anchor="ra")


def tx(t_ms: float, end_ms: float) -> float:
    return AX_X0 + (AX_X1 - AX_X0) * t_ms / end_ms


def draw_strip(c: Canvas, d: dict, t_ms: float, end_ms: float) -> None:
    c.rrect(STRIP, 12, fill=PANEL, outline=BORDER, width=1)
    c.text(STRIP[0] + 20, STRIP[1] + 12, "Events", font("bold", 22), INK)
    c.text(STRIP[0] + 20 + tw("Events", font("bold", 22)) + 10, STRIP[1] + 16,
           "seconds since session start", font("regular", 18), INK_2)
    f_lab = font("regular", 20)
    # axis: elapsed part dark, rest light
    now_x = tx(min(t_ms, end_ms), end_ms)
    c.line([(AX_X0, AX_Y), (AX_X1, AX_Y)], TRACK, width=3)
    c.line([(AX_X0, AX_Y), (now_x, AX_Y)], INK_3, width=3)
    for s in range(0, int(end_ms // 1000) + 1):
        x = tx(s * 1000, end_ms)
        c.line([(x, AX_Y + 4), (x, AX_Y + 9)], INK_3, width=1)
        if s % 2 == 0:
            c.text(x, AX_Y + 12, f"{s} s", font("regular", 17), INK_3, anchor="ma")
    ticks = [
        # label, time, color, row, side
        (f"toolCall {secs(d['tool_call_ms'])}", d["tool_call_ms"], BLUE, 3, "left"),
        (f"stop (speech onset) {secs(d['stop_ms'])}", d["stop_ms"], INK, 2, "left"),
        (f"interrupted {secs(d['interrupted_ms'])} (+{d['interrupted_ms'] - d['stop_ms']} ms)",
         d["interrupted_ms"], BLUE, 1, "right"),
        # tool response is 5 ms after the commit: draw it first (ring marker) so
        # the commit's green leader and dot stay visible on top of it
        (f"tool response {secs(d['response_ms'])}", d["response_ms"], INK, 2, "right"),
        (f"committed {secs(d['committed_ms'])}", d["committed_ms"], GREEN, 3, "left"),
    ]
    for label, at, color, row, side in ticks:
        if t_ms < at:
            continue
        x = tx(at, end_ms)
        ly = ROW_Y[row]
        c.line([(x, AX_Y), (x, ly + 2)], color, width=2)
        if side == "right":
            c.text(x + 8, ly, label, f_lab, INK)
        else:
            c.text(x - 8, ly, label, f_lab, INK, anchor="ra")
    for label, at, color, row, side in ticks:
        if t_ms < at:
            continue
        if at == d["response_ms"]:
            c.dot(tx(at, end_ms), AX_Y, 10, PANEL)
            c.ring(tx(at, end_ms), AX_Y, 10, color, width=2)
        else:
            c.dot(tx(at, end_ms), AX_Y, 6, color, ring=PANEL)
    # the cancellation that never came (only claimed once the session closed)
    if t_ms >= d["session_closed_ms"] and d["n_cancellations"] == 0:
        s = "toolCallCancellation: never received"
        fb = font("bold", 20)
        c.text(AX_X1, ROW_Y[1], s, fb, INK, anchor="ra")
        c.ring(AX_X1 - tw(s, fb) - 16, ROW_Y[1] + 11, 7, INK, width=2)
    # playhead
    c.poly([(now_x - 7, AX_Y - 20), (now_x + 7, AX_Y - 20), (now_x, AX_Y - 10)], INK)


def render(d: dict, t_ms: float, end_ms: float) -> Image.Image:
    c = Canvas()
    draw_header(c, t_ms)
    draw_conversation(c, d, t_ms)
    draw_service(c, d, t_ms)
    draw_strip(c, d, t_ms, end_ms)
    return c.frame()


# ---------------------------------------------------------------- main
def main() -> None:
    d = load_run()
    model_s = wav_seconds(MODEL_WAV)
    model_end_ms = d["model_first_chunk_ms"] + model_s * 1000
    end_ms = model_end_ms + TAIL_MS
    n_run = math.ceil(end_ms / 1000 * FPS)
    n_hold = int(round(HOLD_S * FPS))
    n_frames = n_run + n_hold
    total_s = n_frames / FPS

    print(f"run {RUN}: book {d['book_ms']} ms, toolCall {d['tool_call_ms']} ms, stop {d['stop_ms']} ms, "
          f"interrupted {d['interrupted_ms']} ms, committed {d['committed_ms']} ms, "
          f"tool response {d['response_ms']} ms ({d['response']}), model text {d['model_text_ms']} ms, "
          f"model audio {d['model_first_chunk_ms']}-{model_end_ms:.0f} ms, cancellations {d['n_cancellations']}")
    print(f"model said: {d['model_text']!r}")
    print(f"timeline 0-{end_ms:.0f} ms, {n_run} frames + {n_hold} hold = {total_s:.3f} s")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        mix_path = Path(tmp) / "mix.wav"
        peak = build_mix(d, total_s, mix_path)
        print(f"audio mix: raw peak {peak:.3f}, normalized to -1 dBFS")
        cmd = [FFMPEG, "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
               "-i", str(mix_path),
               "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-ar", str(SR), "-ac", "1",
               "-movflags", "+faststart", str(OUT_MP4)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for i in range(n_frames):
            t_ms = min(i * 1000 / FPS, end_ms)
            proc.stdin.write(render(d, t_ms, end_ms).tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            raise SystemExit("ffmpeg (mp4) failed")

    vf = (f"fps={GIF_FPS},scale={GIF_W}:-1:flags=lanczos,split[a][b];"
          "[a]palettegen=max_colors=256:stats_mode=full:reserve_transparent=0[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(OUT_MP4), "-an", "-vf", vf, str(OUT_GIF)], check=True)
    for p in (OUT_MP4, OUT_GIF):
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
