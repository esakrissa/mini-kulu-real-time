"""Render the latency charts in docs/img/ from a per-turn JSONL file (docs/data/*.jsonl).

    uv run --no-project --with matplotlib python scripts/plot_latency.py docs/data/cloud-30-turns.jsonl

Writes a light and a dark PNG of each chart, for GitHub's <picture> theme switch:
  turn-latency-over-call-{light,dark}.png   e2e and LLM first token per turn, CPU-starved turns shaded
  stage-latency-healthy-vs-starved-{light,dark}.png   per-stage p50, before vs after the VAD fell behind
"""

import json
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"

# Categorical slots 1-2 of the reference palette, validated for both surfaces.
THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "text": "#0b0b0b",
        "muted": "#52514e",
        "grid": "#e4e3df",
        "shade": "#f1efe9",
        "s1": "#2a78d6",
        "s2": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19",
        "text": "#f0efec",
        "muted": "#c3c2b7",
        "grid": "#33332f",
        "shade": "#262624",
        "s1": "#3987e5",
        "s2": "#d95926",
    },
}
STAGES = [
    ("stt_final", "STT final transcript"),
    ("end_of_turn", "End-of-turn decision"),
    ("llm_ttft", "LLM first token"),
    ("tts_ttfb", "TTS first audio"),
    ("e2e", "End to end"),
]


def style(ax, t):
    ax.set_facecolor(t["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(t["grid"])
    ax.tick_params(colors=t["muted"], labelsize=10, length=0)
    ax.grid(axis="y" if ax.name == "rectilinear" else "both", color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def over_call(turns, t, path):
    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=200, facecolor=t["surface"])
    style(ax, t)
    x = [r["turn"] for r in turns]
    lag = [r["turn"] for r in turns if r["vad_lagging"]]
    if lag:
        ax.axvspan(lag[0] - 0.5, x[-1] + 0.5, color=t["shade"], zorder=0)
        ax.text(
            lag[0], 3.55, "VAD falling behind real time (shared-core CPU exhausted)",
            color=t["muted"], fontsize=9.5, va="top",
        )
    for key, label, color in (("e2e", "End to end", t["s1"]), ("llm_ttft", "LLM first token", t["s2"])):
        y = [r[key] for r in turns]
        ax.plot(x, y, color=color, linewidth=2, marker="o", markersize=5,
                markeredgecolor=t["surface"], markeredgewidth=1.5, label=label, zorder=3)
        ax.annotate(label, (x[-1], y[-1]), xytext=(8, 0), textcoords="offset points",
                    color=t["text"], fontsize=10, va="center")
    ax.set_xlim(0.5, x[-1] + 4)
    ax.set_ylim(0, 3.6)
    ax.set_xticks([1, 5, 10, 15, 20, 25, 30])
    ax.set_xlabel("Turn in a 30-turn call", color=t["muted"], fontsize=10)
    ax.set_ylabel("Seconds", color=t["muted"], fontsize=10)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False,
              labelcolor=t["text"], fontsize=10, borderaxespad=0.2)
    fig.suptitle("Turn latency across one call on the free e2-micro", x=0.065, ha="left",
                 color=t["text"], fontsize=13, fontweight="bold")
    fig.text(0.065, 0.885, "Agent-side, per turn. Spikes to ~2.9 s in the first half are the turn "
             "detector waiting out pauses in the synthetic speech.", color=t["muted"], fontsize=9.5)
    fig.subplots_adjust(left=0.065, right=0.97, top=0.76, bottom=0.13)
    fig.savefig(path, facecolor=t["surface"])
    plt.close(fig)


def healthy_vs_starved(turns, t, path):
    groups = (
        ("Healthy CPU", [r for r in turns if not r["vad_lagging"]], t["s1"]),
        ("VAD lagging", [r for r in turns if r["vad_lagging"]], t["s2"]),
    )
    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=200, facecolor=t["surface"])
    style(ax, t)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=t["grid"], linewidth=0.8)
    h = 0.36
    for gi, (label, rows, color) in enumerate(groups):
        for si, (key, _) in enumerate(STAGES):
            v = statistics.median(r[key] for r in rows)
            y = si + (gi - 0.5) * (h + 0.04)
            ax.barh(y, v, height=h, color=color, label=f"{label} (n={len(rows)})" if si == 0 else None)
            ax.text(v + 0.03, y, f"{v:.2f} s", va="center", color=t["text"], fontsize=9.5)
    ax.set_yticks(range(len(STAGES)))
    ax.set_yticklabels([s[1] for s in STAGES], color=t["text"], fontsize=10)
    ax.invert_yaxis()
    ax.set_xlim(0, max(statistics.median(r["e2e"] for r in g[1]) for g in groups) * 1.18)
    ax.set_xlabel("Median seconds (p50)", color=t["muted"], fontsize=10)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False,
              labelcolor=t["text"], fontsize=10, borderaxespad=0.2)
    fig.suptitle("Where the time goes: per-stage p50, healthy vs CPU-starved turns", x=0.2, ha="left",
                 color=t["text"], fontsize=13, fontweight="bold")
    fig.subplots_adjust(left=0.2, right=0.97, top=0.82, bottom=0.12)
    fig.savefig(path, facecolor=t["surface"])
    plt.close(fig)


def main() -> None:
    turns = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
    OUT.mkdir(parents=True, exist_ok=True)
    for mode, t in THEMES.items():
        over_call(turns, t, OUT / f"turn-latency-over-call-{mode}.png")
        healthy_vs_starved(turns, t, OUT / f"stage-latency-healthy-vs-starved-{mode}.png")
    print(f"wrote 4 charts to {OUT}")


if __name__ == "__main__":
    main()
