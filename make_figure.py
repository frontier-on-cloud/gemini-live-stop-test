"""Timeline figure: what happened after "stop", scenario A run 1 vs scenario C run 1.

    uv run --with matplotlib python make_figure.py

Reads the JSONL timelines in results/ and writes
results/figures/stop-timeline-A1-C1.png (1600x900) and .svg.
Every timestamp on the figure is read from the files; nothing is typed in by hand.
"""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
OUT = RESULTS / "figures" / "stop-timeline-A1-C1"

PANELS = [
    ("A_default_stop1.0_nohonor.jsonl", 1, "Default (NON_BLOCKING), run 1"),
    ("C_blocking_stop1.0_honor.jsonl", 1, "behavior: BLOCKING, run 1"),
]
# The first D pass is superseded by D_rerun (harness stop-condition bug, see summary.md).
SUPERSEDED = {"D_default_stop5.5_honor.jsonl"}

# Placement per panel: event key -> (level, side, wrap). level > 0 is above the line
# (app side: user messages, booking service), level < 0 below (Live API events).
# side "left" puts the text right of the marker, "right" puts it left of it.
# wrap puts each part of the small second label on its own line.
LAYOUT = {
    "A": {"request": (1, "left", False), "tool_call_1": (-1, "right", True),
          "stop": (1, "left", False), "speech": (-1, "left", False), "commit_1": (1, "left", False)},
    "C": {"request": (1, "left", False), "tool_call_1": (-1, "right", True),
          "stop": (1, "left", False), "interrupted": (-2, "left", False),
          "tool_call_2": (-1, "left", True), "commit_1": (1, "left", False),
          "commit_2": (2, "right", False), "speech": (-1, "right", False)},
}
LEVEL_Y = {1: 1.0, 2: 2.2, -1: -1.0, -2: -2.2}

# Palette: three categorical slots (validated all-pairs) + grey chrome.
SURFACE, INK, INK2 = "#fcfcfb", "#0b0b0b", "#52514e"
GRID, BASELINE = "#e1e0d9", "#c3c2b7"
USER, API, SERVICE = "#eb6834", "#2a78d6", "#1baf7a"
MARKER = {"user": ("s", USER), "api": ("o", API), "service": ("D", SERVICE)}

X_MAX = 7.5
QUOTE_MAX = 60

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "svg.fonttype": "path",
    "text.color": INK,
})


def load(name, run):
    rows = [json.loads(line) for line in (RESULTS / name).read_text().splitlines() if line.strip()]
    return [r for r in rows if r["run"] == run]


def secs(ms):
    return f"{ms / 1000:.3f} s"


def events_for(rows):
    """Return [(key, t_ms, actor, main_label, [sub_parts])] for the events the figure shows."""
    def first(event, **match):
        return next((r for r in rows if r["event"] == event
                     and all(r.get(k) == v for k, v in match.items())), None)

    out = []
    req = first("user_text_sent", label="book_request")
    if req:
        out.append(("request", req["t_ms"], "user", "user: book 3pm", [secs(req["t_ms"])]))
    stop = first("user_text_sent", label="stop")
    stop_ms = stop["t_ms"] if stop else None
    if stop:
        out.append(("stop", stop_ms, "user", "user: stop", [secs(stop_ms)]))
    for i, tc in enumerate((r for r in rows if r["event"] == "tool_call_received"), 1):
        out.append((f"tool_call_{i}", tc["t_ms"], "api", f"tool call #{i}",
                    [secs(tc["t_ms"]), f"id {tc['call_id']}"]))
    intr = first("interrupted")
    if intr:
        sub = [secs(intr["t_ms"])]
        if stop_ms is not None:
            sub.append(f"{intr['t_ms'] - stop_ms} ms after stop")
        out.append(("interrupted", intr["t_ms"], "api", "interrupted", sub))
    commits = [r for r in rows if r["event"] == "service_committed"]
    for i, c in enumerate(commits, 1):
        main = "booking committed" if len(commits) == 1 else f"booking #{i} committed"
        out.append((f"commit_{i}", c["t_ms"], "service", main, [secs(c["t_ms"])]))
    if stop_ms is not None:
        chunks = [r for r in rows if r["event"] == "model_transcript" and r["t_ms"] >= stop_ms]
        if chunks:
            text = "".join(c["text"] for c in chunks).strip()
            if len(text) > QUOTE_MAX:  # cut at the last word boundary within QUOTE_MAX
                text = text[:QUOTE_MAX + 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
            out.append(("speech", chunks[0]["t_ms"], "api", f"“{text}”",
                        ["model speech (transcript)", secs(chunks[0]["t_ms"])]))
    return out, stop_ms


def count_cancellations():
    sessions = cancels = 0
    for path in sorted(RESULTS.glob("*.jsonl")):
        if path.name in SUPERSEDED:
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        sessions += sum(r["event"] == "run_start" for r in rows)
        cancels += sum(r["event"] == "tool_call_cancellation" for r in rows)
    return cancels, sessions


def draw_panel(ax, key, title, rows):
    evs, stop_ms = events_for(rows)
    layout = LAYOUT[key]
    labels, stems = [], []
    backing = dict(boxstyle="square,pad=0.08", fc=SURFACE, ec="none")  # hides gridlines under text

    ax.set_xlim(0, X_MAX)
    ax.set_ylim(-2.95, 2.95)
    top = 0.93  # vertical rules stop short of the panel title
    for s in range(1, int(X_MAX) + 1):
        ax.axvline(s, ymax=top, color=GRID, lw=1, zorder=0)
    ax.axhline(0, color=BASELINE, lw=2, zorder=1, solid_capstyle="butt")
    if stop_ms is not None:
        stop_line = ax.axvline(stop_ms / 1000, ymax=top, color=INK2, lw=1.3, ls=(0, (5, 4)),
                               zorder=2)
        stems.append(("stop_line", stop_line))

    for ev_key, t_ms, actor, main, sub in evs:
        if ev_key not in layout:
            print(f"warning: no layout for {key}:{ev_key}, skipped", file=sys.stderr)
            continue
        level, side, wrap = layout[ev_key]
        x, y = t_ms / 1000, LEVEL_Y[level]
        near_stop = stop_ms is not None and abs(t_ms - stop_ms) <= 50
        if not near_stop:  # the dashed stop line already serves as the stem there
            (stem,) = ax.plot([x, x], [0, y], color=BASELINE, lw=1.2, zorder=1)
            stems.append((ev_key, stem))
        shape, color = MARKER[actor]
        ax.plot(x, y, marker=shape, ms=13 if shape != "D" else 11, color=color,
                mec=SURFACE, mew=2, zorder=4)
        dx = 12 if side == "left" else -12
        ha = "left" if side == "left" else "right"
        t_main = ax.annotate(main, (x, y), xytext=(dx, 2), textcoords="offset points",
                             ha=ha, va="bottom", fontsize=17, color=INK, zorder=5,
                             annotation_clip=False, bbox=backing)
        t_sub = ax.annotate(("\n" if wrap else ", ").join(sub), (x, y), xytext=(dx, -3),
                            textcoords="offset points", ha=ha, va="top", fontsize=13,
                            color=INK2, zorder=5, annotation_clip=False, bbox=backing,
                            multialignment=ha, linespacing=1.25)
        labels += [(ev_key, t_main), (ev_key, t_sub)]

    ax.set_yticks([])
    for side in ("left", "right", "top", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="x", length=0, labelsize=13, labelcolor=INK2, pad=8)
    t_title = ax.text(0, 1.0, title, transform=ax.transAxes, ha="left", va="bottom",
                      fontsize=18, fontweight="bold", color=INK)
    n_calls = sum(e[0].startswith("tool_call_") for e in evs)
    n_commits = sum(e[0].startswith("commit_") for e in evs)
    words = {0: "no", 1: "one", 2: "two", 3: "three"}
    facts = [f"{words.get(n_calls, n_calls)} tool call{'s' * (n_calls != 1)}",
             f"{words.get(n_commits, n_commits)} booking{'s' * (n_commits != 1)} committed"]
    if not any(e[0] == "interrupted" for e in evs):
        facts.append("no interrupted event")
    t_facts = ax.annotate(", ".join(facts), xy=(1, 0), xycoords=t_title, xytext=(12, 0),
                          textcoords="offset points", ha="left", va="bottom",
                          fontsize=15, color=INK2)
    labels += [("title", t_title), ("facts", t_facts)]
    return labels, stems


def check_overlaps(fig, all_labels, all_stems):
    """Fail loudly if any two labels overlap or a stem runs through another event's label."""
    renderer = fig.canvas.get_renderer()
    boxes = [(k, t.get_window_extent(renderer)) for k, t in all_labels]
    problems = []
    for i, (ka, a) in enumerate(boxes):
        for kb, b in boxes[i + 1:]:
            if a.overlaps(b):
                problems.append(f"labels overlap: {ka} / {kb}")
        for ks, stem in all_stems:
            if ks != ka and a.overlaps(stem.get_window_extent(renderer).padded(1)):
                problems.append(f"stem of {ks} crosses label of {ka}")
        if a.x0 < 0 or a.x1 > fig.bbox.width:
            problems.append(f"label {ka} runs off the figure")
    return problems


def main():
    fig = plt.figure(figsize=(16, 9), dpi=100, facecolor=SURFACE)
    axes = [fig.add_axes([0.075, 0.54, 0.885, 0.305], facecolor=SURFACE),
            fig.add_axes([0.075, 0.175, 0.885, 0.305], facecolor=SURFACE)]

    all_labels, all_stems = [], []
    for ax, (name, run, title) in zip(axes, PANELS):
        labels, stems = draw_panel(ax, name[0], title, load(name, run))
        all_labels += [(f"{name[0]}:{k}", t) for k, t in labels]
        all_stems += [(f"{name[0]}:{k}", s) for k, s in stems]

    ticks = [i * 0.5 for i in range(int(X_MAX * 2) + 1)]
    for ax in axes:
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t:g} s" if t == int(t) else "" for t in ticks])
    axes[0].tick_params(labelbottom=False)
    axes[1].set_xlabel("seconds since session start", fontsize=13, color=INK2, labelpad=6)

    fig.text(0.075, 0.957, "Gemini 3.8 Live, gemini-3.8-live, 2026-09-29: what happened after “stop”",
             ha="left", va="center", fontsize=24, fontweight="bold", color=INK)

    handles = [
        Line2D([], [], ls="none", marker="s", ms=12, color=USER, mec=SURFACE, mew=2, label="user message"),
        Line2D([], [], ls="none", marker="D", ms=10, color=SERVICE, mec=SURFACE, mew=2,
               label="booking service"),
        Line2D([], [], ls="none", marker="o", ms=12, color=API, mec=SURFACE, mew=2, label="Live API event"),
        Line2D([], [], color=INK2, lw=1.3, ls=(0, (5, 4)), label="stop sent"),
    ]
    fig.legend(handles=handles, loc="center left", bbox_to_anchor=(0.068, 0.908), ncol=4,
               frameon=False, fontsize=14, labelcolor=INK2, handlelength=1.6,
               handletextpad=0.5, columnspacing=2.2)

    cancels, sessions = count_cancellations()
    received = "never received" if cancels == 0 else f"received {cancels} times"
    fig.text(0.075, 0.072, f"toolCallCancellation: {received} ({cancels} of {sessions} sessions)",
             ha="left", va="center", fontsize=15, color=INK)
    fig.text(0.075, 0.035,
             "fake booking service, 4 s latency, text input, audio output with transcription, "
             "google-genai 2.25.0. github.com/frontier-on-cloud/gemini-live-stop-test",
             ha="left", va="center", fontsize=12, color=INK2)

    fig.canvas.draw()
    problems = check_overlaps(fig, all_labels, all_stems)
    for p in problems:
        print("layout problem:", p, file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT.with_suffix(".png"), dpi=100, facecolor=SURFACE)
    fig.savefig(OUT.with_suffix(".svg"), facecolor=SURFACE)
    print(f"wrote {OUT.with_suffix('.png').relative_to(ROOT)} and .svg")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
