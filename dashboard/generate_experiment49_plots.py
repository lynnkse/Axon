#!/usr/bin/env python3
"""Regenerate the four Experiment 49 planning-profile figures from raw audit data.

The layout deliberately keeps a single figure title and a separate subtitle so
that the session label cannot overlap the metadata line in the dashboard.
"""
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

CAMPAIGN = Path("/lab/shared/anton/experiment49_unified_30run_beta095_20261011")
OUT = Path(__file__).resolve().parent / "assets" / "experiment49"
CASES = ((1, "e065", 0), (21, "e065", 4))


def outcome_label(session):
    return "Consistent planning session" if session["consistent"] else "Inconsistent planning session"


def yielded_action(session):
    """Return the catalog action composed from the two final executed components."""
    executed = session.get("executed_components", {})
    component_indices = {
        robot: component.get("robot_path_index")
        for robot, component in executed.items()
    }
    for candidate in session.get("catalog", []):
        components = candidate.get("components", {})
        if all(
            components.get(robot, {}).get("robot_path_index") == index
            for robot, index in component_indices.items()
        ):
            return candidate["action_id"]["joint_index"], component_indices
    return None, component_indices


def render(seed, label, session_index, robot):
    result_path = CAMPAIGN / "results" / f"experiment49_seed{seed}_{label}_samples10.json"
    result = json.loads(result_path.read_text())["seeds"][0]["result"]
    session = result["sessions"][session_index]
    iteration = 1
    step = session["details"]["communication"][iteration]["evaluation"]["audit"]["robots"][robot]["step_1"]
    values = np.asarray(step["objective_values"]["path_length+entropy"], dtype=float)
    selected = int(step["selected_action"])
    yielded, yielded_components = yielded_action(session)

    fig, ax = plt.subplots(figsize=(12, 7))
    fig.subplots_adjust(top=0.80, bottom=0.12, left=0.10, right=0.98)
    x = np.arange(len(values))
    bars = ax.bar(x, values, color="#b8c4d6", edgecolor="#526579", linewidth=0.8)
    bars[selected].set_color("#2575b7")
    bars[selected].set_edgecolor("#0c3c68")
    if yielded is not None:
        bars[yielded].set_edgecolor("#6d28d9")
        bars[yielded].set_linewidth(2.6)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.012, f"{value:.3f}",
                ha="center", va="bottom", fontsize=10)

    eps = {"e065": ".65", "e075": ".75", "e090": ".90"}[label]
    ax.set_title(f"{outcome_label(session)} — {robot.replace('_', ' ')}", fontsize=17, pad=34, weight="bold")
    fig.text(0.5, 0.890,
             f"Experiment 49  |  $\\epsilon={eps}$  |  seed {seed}  |  physical session {session_index}  |  communication iteration {iteration}",
             ha="center", fontsize=11, color="#374151")
    ax.set_ylabel("Planning objective (path length + entropy)")
    ax.set_xlabel("Joint-action candidate")
    tick_labels = [f"a{i}" for i in x]
    if yielded is not None:
        tick_labels[yielded] += "\nYIELDED"
    ax.set_xticks(x, tick_labels)
    if yielded is not None:
        ax.scatter(yielded, values[yielded], marker="D", s=78, zorder=4,
                   facecolor="#f5f3ff", edgecolor="#6d28d9", linewidth=1.8)
        composition = " + ".join(
            f"{name.replace('_', ' ')}[{index}]"
            for name, index in yielded_components.items()
        )
        ax.annotate(f"yielded a{yielded}: {composition}",
                    xy=(yielded, values[yielded]), xytext=(0, 45),
                    textcoords="offset points", ha="center", va="bottom",
                    fontsize=9, color="#4c1d95", weight="bold",
                    arrowprops={"arrowstyle": "-", "color": "#6d28d9", "lw": 1.1})
    ax.grid(axis="y", alpha=0.22)
    ax.set_axisbelow(True)
    low, high = values.min(), values.max()
    ax.set_ylim(low - max(0.08, (high - low) * 0.35), high + max(0.08, (high - low) * 0.35))
    legend = [plt.Rectangle((0, 0), 1, 1, color="#2575b7", label="robot's local Step-1 selection")]
    if yielded is not None:
        legend.append(plt.Line2D([0], [0], marker="D", color="w", markerfacecolor="#f5f3ff",
                                 markeredgecolor="#6d28d9", markeredgewidth=1.8, markersize=8,
                                 label="yielded joint action after both robots decide"))
    ax.legend(handles=legend, loc="lower right", frameon=True)
    out = OUT / f"e49_{label}_seed{seed}_session{session_index}_iter{iteration}_{robot}.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(out)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for seed, label, session in CASES:
        for robot in ("Robot_A", "Robot_B"):
            render(seed, label, session, robot)
