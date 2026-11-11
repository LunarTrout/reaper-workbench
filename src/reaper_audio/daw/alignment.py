"""Propose geometric moves from measured offsets to the project beat grid.

This does not decide how tight the performance should be. The caller chooses
``strength`` (0 = leave alone, 1 = place on the grid), and the resulting
``move_s`` is passed to the transactional ``item.split`` operation.
"""
from __future__ import annotations

from typing import Iterable, Mapping


def propose_moves(candidates: Iterable[Mapping], *, strength: float = 1.0) -> list[dict]:
    if not 0.0 <= strength <= 1.0:
        raise ValueError("strength must be between 0 and 1")
    moves = []
    for row in candidates:
        time_s = row.get("time_s")
        grid_s = row.get("grid_time_s")
        if not isinstance(time_s, (int, float)) or not isinstance(grid_s, (int, float)):
            continue
        move_s = (float(grid_s) - float(time_s)) * float(strength)
        moves.append({
            "time_s": round(float(time_s), 4),
            "grid_time_s": round(float(grid_s), 4),
            "move_s": round(move_s, 4),
            "group": row.get("group", "unassigned"),
            "tail_s": row.get("tail_s", 0.0),
            "sources": list(row.get("sources") or []),
            "evidence": row.get("reason", ""),
        })
    return moves


def plan_for_group(moves: Iterable[Mapping], group: str) -> list[dict]:
    """Select moves for one explicit group chosen by the agent/user."""
    return [dict(move) for move in moves if move.get("group") == group]
