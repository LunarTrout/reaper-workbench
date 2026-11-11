from reaper_audio.daw.alignment import plan_for_group, propose_moves


ROWS = [
    {"time_s": 1.078, "grid_time_s": 1.0, "group": "guitars",
     "tail_s": 0.15, "sources": ["amp guitar"], "reason": "x"},
    {"time_s": 2.4, "grid_time_s": 2.5, "group": "drums",
     "tail_s": 0.03, "sources": ["kick"], "reason": "y"},
]


def test_move_is_strength_scaled_offset():
    moves = propose_moves(ROWS, strength=0.5)
    assert moves[0]["move_s"] == -0.039
    assert moves[1]["move_s"] == 0.05


def test_strength_is_explicit_and_group_selection_is_explicit():
    assert propose_moves(ROWS, strength=1.0)[1]["move_s"] == 0.1
    assert [row["group"] for row in plan_for_group(propose_moves(ROWS), "drums")] == ["drums"]


def test_rejects_out_of_range_strength():
    try:
        propose_moves(ROWS, strength=1.5)
    except ValueError as exc:
        assert "strength" in str(exc)
    else:
        raise AssertionError("expected ValueError")
