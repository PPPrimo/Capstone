"""
PlotLog.py - standalone viewer for plot_logger .json history files.

Usage:
    python PlotLog.py                                  # list available series
    python PlotLog.py shoulder_pan_position            # plot one series (searches leader + follower)
    python PlotLog.py --role leader shoulder_pan_position  # plot from leader log
    python PlotLog.py --role follower --all            # plot all follower series
    python PlotLog.py --log-dir path/to/log shoulder_pan_position  # custom dir
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt

DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "WebRepo" / "server" / "sensitive" / "log"

COLORS = [
    "#007aff", "#34c759", "#ff3b30", "#ff9500",
    "#af52de", "#5856d6", "#ff6482", "#30b0c7",
]


def load_series(path: Path) -> tuple[list, list]:
    xs, ys = [], []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                xs.append(e["x"])
                ys.append(e["y"])
            except (json.JSONDecodeError, KeyError):
                pass
    return xs, ys


def resolve_log_dir(base: Path, role: str | None) -> Path:
    """Return the log directory for the given role, or base if no role."""
    if role:
        return base / role.lower()
    return base


def plot_one(name: str, log_dir: Path) -> None:
    path = log_dir / f"{name}.json"
    if not path.exists():
        print(f"Not found: {path}")
        return
    xs, ys = load_series(path)
    if not xs:
        print(f"No data in {path}")
        return
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(xs, ys, linewidth=1.2, color=COLORS[0])
    ax.fill_between(xs, ys, alpha=0.10, color=COLORS[0])
    ax.set_title(name.replace("_", " ").title(), fontsize=12)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.grid(True, color="#e8e8e8")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()


def plot_multi(names: list[str], log_dir: Path) -> None:
    n = len(names)
    cols = min(n, 3)
    rows = -(-n // cols)  # ceil division
    fig, axs = plt.subplots(rows, cols, figsize=(6 * cols, 4 * rows), squeeze=False)
    axes = [axs[r][c] for r in range(rows) for c in range(cols)]
    for ax in axes[n:]:
        ax.set_visible(False)
    for idx, name in enumerate(names):
        path = log_dir / f"{name}.json"
        ax = axes[idx]
        color = COLORS[idx % len(COLORS)]
        if not path.exists():
            ax.set_title(f"{name} (not found)", fontsize=9)
            continue
        xs, ys = load_series(path)
        if xs:
            ax.plot(xs, ys, linewidth=1.2, color=color)
            ax.fill_between(xs, ys, alpha=0.10, color=color)
        ax.set_title(name.replace("_", " ").title(), fontsize=10)
        ax.set_xlabel("x")
        ax.set_ylabel("y")
        ax.grid(True, color="#e8e8e8")
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()


def list_series(log_dir: Path) -> list[str]:
    return sorted(p.stem for p in log_dir.glob("*.json"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot recorded motor data from .json log files.")
    parser.add_argument("series", nargs="*", help="Series name(s) to plot (without .json extension)")
    parser.add_argument("--all", action="store_true", help="Plot all available series in one figure")
    parser.add_argument("--role", choices=["leader", "follower"], default=None,
                        help="Robot role — resolves to log/leader/ or log/follower/ subdirectory")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="Base log directory")
    args = parser.parse_args()

    log_dir = resolve_log_dir(Path(args.log_dir), args.role)
    if not log_dir.exists():
        print(f"Log directory not found: {log_dir}")
        if args.role is None:
            print("Tip: use --role leader or --role follower to target a subdirectory.")
        sys.exit(1)

    available = list_series(log_dir)

    if not args.series and not args.all:
        if available:
            role_label = f" ({args.role})" if args.role else ""
            print(f"Available series{role_label}:")
            for s in available:
                print(f"  {s}")
        else:
            print(f"No .json files found in {log_dir}")
        sys.exit(0)

    names = available if args.all else args.series

    if len(names) == 1:
        plot_one(names[0], log_dir)
    else:
        plot_multi(names, log_dir)

    plt.show()


if __name__ == "__main__":
    main()
