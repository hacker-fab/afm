#!/usr/bin/env python3
"""Plot a Siglent trend CSV while excluding the obvious acquisition outlier."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def read_trend(path: Path) -> dict[str, np.ndarray]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    if not rows:
        raise ValueError(f"No data rows found in {path}")

    return {
        "frame": np.array([int(row["frame"]) for row in rows]),
        "elapsed_s": np.array([float(row["elapsed_s"]) for row in rows]),
        "differential_mv": np.array(
            [float(row["differential_mean_v"]) * 1_000.0 for row in rows]
        ),
        "total_v": np.array([float(row["total_mean_v"]) for row in rows]),
    }


def robust_outlier_mask(total_v: np.ndarray) -> np.ndarray:
    """Flag gross acquisition faults using the robust spread of total signal."""
    median = np.median(total_v)
    mad = np.median(np.abs(total_v - median))
    if mad == 0:
        return np.zeros(total_v.shape, dtype=bool)
    robust_z = np.abs(total_v - median) / (1.4826 * mad)
    return robust_z > 15.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    data = read_trend(args.csv_path)
    excluded = robust_outlier_mask(data["total_v"])
    kept = ~excluded

    # NaNs preserve a visible break across the rejected interval.
    differential_mv = np.where(kept, data["differential_mv"], np.nan)
    total_v = np.where(kept, data["total_v"], np.nan)

    fig, (axis, total_axis) = plt.subplots(
        2, 1, figsize=(12, 7.2), sharex=True, constrained_layout=True
    )
    axis.plot(data["elapsed_s"], differential_mv, color="#2563eb", linewidth=1.25)
    total_axis.plot(data["elapsed_s"], total_v, color="#dc2626", linewidth=1.25)

    axis.set_title("Siglent settling trend")
    axis.set_ylabel("Differential mean (mV)")
    total_axis.set_ylabel("CH1 + CH2 + CH3 + CH4 mean (V)")
    total_axis.set_xlabel("Elapsed time (s)")

    for plot_axis in (axis, total_axis):
        plot_axis.grid(True, alpha=0.25)

    if excluded.any():
        start = float(data["elapsed_s"][excluded][0])
        end = float(data["elapsed_s"][excluded][-1])
        for plot_axis in (axis, total_axis):
            plot_axis.axvspan(start, end, color="#9ca3af", alpha=0.18, linewidth=0)
        axis.text(
            (start + end) / 2,
            0.97,
            "excluded acquisition outlier",
            transform=axis.get_xaxis_transform(),
            ha="center",
            va="top",
            fontsize=9,
            color="#4b5563",
        )

    output = args.output or args.csv_path.with_name(f"{args.csv_path.stem}_filtered.png")
    fig.savefig(output, dpi=180, facecolor="white")
    plt.close(fig)

    excluded_frames = data["frame"][excluded]
    if excluded_frames.size:
        print(
            f"Saved {output}; excluded {excluded_frames.size} frames "
            f"({excluded_frames[0]}-{excluded_frames[-1]})."
        )
    else:
        print(f"Saved {output}; no gross outliers detected.")


if __name__ == "__main__":
    main()
