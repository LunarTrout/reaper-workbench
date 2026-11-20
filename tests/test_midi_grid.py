"""Unit tests for the musical-grid MIDI core (no bridge, no REAPER)."""

from __future__ import annotations

import pytest

from reaper_audio.daw.midi import (MidiItem, MidiService, TempoMap, generate,
                                   grid_check, item_statistics, replace_notes)
from reaper_audio.errors import BridgeUnavailable, ValidationError


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def tempo_context(markers=(), *, bpm=120.0, numerator=4, denominator=4):
    return {"bpm": bpm, "numerator": numerator, "denominator": denominator,
            "markers": list(markers)}


def marker(time_s, measure, beat, bpm, *, numerator=4, denominator=4, linear=False):
    return {"time_s": time_s, "measure": measure, "beat": beat, "bpm": bpm,
            "numerator": numerator, "denominator": denominator,
            "linear": linear}


def note_context(start_s, end_s, pitch, velocity, *, start_ppq=None, channel=0,
                 muted=False):
    note = {"start_s": start_s, "end_s": end_s, "pitch": pitch,
            "velocity": velocity, "channel": channel, "muted": muted}
    if start_ppq is not None:
        note["start_ppq"] = start_ppq
        note["end_ppq"] = start_ppq + (end_s - start_s)
    return note


def item_context(guid="item-1", *, position_s=0.0, length_s=8.0,
                 start_offset_s=0.0, playrate=1.0, notes=(), is_midi=True):
    return {
        "guid": guid,
        "position_s": position_s,
        "length_s": length_s,
        "start_offset_s": start_offset_s,
        "playrate": playrate,
        "active_take": {"is_midi": is_midi,
                        "midi": {"note_count": len(notes),
                                 "notes": list(notes)}},
    }


def project_context(item, tempo, *, track_name="drums", extra_items=()):
    return {"tempo": tempo,
            "tracks": [{"guid": "track-1", "name": track_name,
                        "items": [item, *extra_items]}]}


def midi_item(item, tempo):
    return MidiItem.from_context(item, TempoMap.from_context(tempo))


class FakeBridge:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def call(self, operation, payload):
        self.calls.append((operation, payload))
        if self.response is not None:
            return self.response
        return {"inserted": len(payload["notes"]), "removed": 3}


class FakeSession:
    def __init__(self, context, bridge=None):
        self.context = context
        self.bridge = bridge or FakeBridge()

    def snapshot(self, **kwargs):
        assert kwargs.get("include_items") is True
        assert kwargs.get("include_midi_notes") is True
        return self.context


class RecordingRenderer:
    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"path": "preview.wav"}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# ---------------------------------------------------------------------------
# tempo map
# ---------------------------------------------------------------------------

def test_tempo_map_constant_bars_and_inverse():
    tempo = TempoMap.from_context(tempo_context())
    assert tempo.source == "project.base_tempo_no_markers"
    assert tempo.bar_start_qn(1) == 0.0
    assert tempo.bar_start_qn(2) == 4.0
    assert tempo.bar_start_qn(3) == 8.0
    assert tempo.bar_length_qn(2) == 4.0
    assert tempo.qn_to_time(8.0) == pytest.approx(4.0)
    assert tempo.time_to_qn(4.0) == pytest.approx(8.0)
    assert tempo.qn_to_bar_beat(9.5) == (3, 1.5)


def test_tempo_map_respects_three_four_meter():
    tempo = TempoMap.from_context(tempo_context(numerator=3, denominator=4))
    assert tempo.bar_start_qn(2) == pytest.approx(3.0)
    assert tempo.bar_start_qn(3) == pytest.approx(6.0)
    assert tempo.meter_at_bar(1) == (3, 4, 1.0, 3.0)


def test_tempo_map_changes_bpm_and_converts_qn_exactly():
    tempo = TempoMap.from_context(tempo_context([
        marker(0.0, 0, 0.0, 120.0),
        marker(2.0, 1, 0.0, 60.0),
    ]))
    assert tempo.qn_to_time(0.0) == pytest.approx(0.0)
    assert tempo.qn_to_time(4.0) == pytest.approx(2.0)
    assert tempo.qn_to_time(6.0) == pytest.approx(4.0)
    assert tempo.qn_to_time(8.0) == pytest.approx(6.0)
    assert tempo.time_to_qn(4.0) == pytest.approx(6.0)


def test_tempo_map_inherits_zero_bpm_and_zero_meter_fields():
    tempo = TempoMap.from_context(tempo_context([
        marker(0.0, 0, 0.0, 120.0),
        {"time_s": 2.0, "measure": 1, "beat": 0.0, "bpm": 0,
         "numerator": 0, "denominator": 0, "linear": False},
    ]))
    assert tempo.points[1].bpm == 120.0
    assert tempo.points[1].numerator == 4


def test_tempo_map_refuses_markers_that_disagree_with_music_time():
    with pytest.raises(BridgeUnavailable, match="inconsistent"):
        TempoMap.from_context(tempo_context([
            marker(0.0, 0, 0.0, 120.0),
            marker(2.0, 2, 0.0, 120.0),
        ]))


def test_tempo_map_handles_a_marker_without_a_bar_position():
    tempo = TempoMap.from_context(tempo_context([
        marker(0.0, 0, 0.0, 120.0),
        {"time_s": 2.0, "measure": -1, "beat": -1, "bpm": 60.0,
         "numerator": 0, "denominator": 0, "linear": False},
        marker(6.0, 2, 0.0, 60.0),
    ]))
    assert tempo.points[1].qn == pytest.approx(4.0)
    assert tempo.points[1].measure == -1
    assert tempo.qn_to_time(6.0) == pytest.approx(4.0)
    assert tempo.time_to_qn(4.0) == pytest.approx(6.0)
    assert tempo.bar_start_qn(3) == pytest.approx(8.0)


def test_tempo_map_refuses_a_meter_change_without_a_bar_position():
    with pytest.raises(BridgeUnavailable, match="without a bar position"):
        TempoMap.from_context(tempo_context([
            marker(0.0, 0, 0.0, 120.0),
            {"time_s": 2.0, "measure": -1, "beat": -1, "bpm": 120.0,
             "numerator": 3, "denominator": 4, "linear": False},
        ]))


def test_tempo_map_linear_ramp_round_trips():
    tempo = TempoMap.from_context(tempo_context([
        marker(0.0, 0, 0.0, 100.0, linear=True),
        marker(4.0, 2, 0.0, 140.0),
    ]))
    for qn in (0.0, 1.0, 2.5, 4.0, 6.0, 8.0):
        assert tempo.time_to_qn(tempo.qn_to_time(qn)) == pytest.approx(qn)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def test_generate_follows_tempo_change_inside_fragment():
    tempo = tempo_context([
        marker(0.0, 0, 0.0, 120.0),
        marker(2.0, 1, 0.0, 60.0),
    ])
    item = midi_item(item_context(length_s=6.0), tempo)
    phrase = generate(
        item,
        {"notes": [{"beat": float(beat), "pitch": 36, "velocity": 100}
                   for beat in range(4)],
         "length_beats": 4},
        1, 2, 1,
    )
    assert [round(note.start_s, 6) for note in phrase.notes] == [
        0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
    report = grid_check(generate_item_with(item, phrase), 0.001, 1)
    assert report.notes_beyond_tolerance == 0
    assert report.max_abs_deviation_ms <= 0.001


def generate_item_with(item, phrase):
    from dataclasses import replace

    return replace(item, notes=phrase.notes)


def test_generate_repeats_pattern_and_applies_accents():
    item = midi_item(item_context(length_s=4.0), tempo_context())
    phrase = generate(
        item,
        {"notes": [{"beat": 0, "pitch": 36, "velocity": 100},
                   {"beat": 2, "pitch": 36, "velocity": 90}],
         "length_beats": 4},
        1, 2, 1,
        accents={"beats": [0], "velocity_delta": 10},
    )
    assert len(phrase.notes) == 4
    assert [note.velocity for note in phrase.notes] == [110, 90, 110, 90]
    assert [note.bar for note in phrase.notes] == [1, 1, 2, 2]


def test_generate_uses_the_meter_not_four_beats():
    item = midi_item(item_context(length_s=3.0), tempo_context(numerator=3))
    phrase = generate(item, {"notes": [
        {"beat": 0, "pitch": 36, "velocity": 100},
        {"beat": 1, "pitch": 38, "velocity": 100},
        {"beat": 2, "pitch": 38, "velocity": 100}],
        "length_beats": 3}, 1, 2, 1)
    assert [note.bar for note in phrase.notes] == [1, 1, 1, 2, 2, 2]
    assert [note.start_s for note in phrase.notes] == pytest.approx(
        [0.0, 0.5, 1.0, 1.5, 2.0, 2.5])


def test_generate_treats_a_bare_list_as_one_non_repeating_statement():
    item = midi_item(item_context(length_s=8.0), tempo_context())
    phrase = generate(item, [{"beat": float(beat), "pitch": 36, "velocity": 100}
                             for beat in range(4)], 1, 4, 1)
    assert [note.start_s for note in phrase.notes] == pytest.approx(
        [0.0, 0.5, 1.0, 1.5])


def test_generate_accounts_for_take_offset():
    item = midi_item(
        item_context(position_s=4.0, length_s=4.0, start_offset_s=1.0),
        tempo_context(),
    )
    phrase = generate(item, {"notes": [{"beat": 0, "pitch": 36, "velocity": 100}],
                             "length_beats": 4}, 3, 3, 1)
    assert item.take_offset_qn == pytest.approx(2.0)
    assert phrase.take_offset_qn == pytest.approx(2.0)
    assert phrase.notes[0].start_s == pytest.approx(4.0)
    # The note is the first beat of bar 3 on the project clock, which is PPQ 2
    # in the take because the item skips one source second (two QN).
    assert phrase.notes[0].start_qn == pytest.approx(2.0)


def test_item_without_notes_measures_take_offset_from_its_own_start():
    tempo = tempo_context([
        marker(0.0, 0, 0.0, 120.0),
        marker(2.0, 1, 0.0, 60.0),
    ])
    item = midi_item(item_context(position_s=4.0, length_s=4.0,
                                  start_offset_s=1.0), tempo)
    # QN(4 s) = 6, QN(3 s) = 5; the second is the local tempo, not 2 s at 120.
    assert item.take_offset_qn == pytest.approx(1.0)


def test_generate_refuses_bars_outside_the_item():
    item = midi_item(item_context(position_s=4.0, length_s=2.0), tempo_context())
    with pytest.raises(ValidationError, match="not inside the item"):
        generate(item, [{"beat": 0, "pitch": 36, "velocity": 100}], 1, 1, 1)


def test_generate_rejects_pattern_beats_off_the_subdivision():
    item = midi_item(item_context(), tempo_context())
    with pytest.raises(ValidationError, match="subdivision grid"):
        generate(item, [{"beat": 0.3, "pitch": 36, "velocity": 100}], 1, 1, 4)


def test_generate_clips_same_pitch_collisions():
    item = midi_item(item_context(length_s=4.0), tempo_context())
    phrase = generate(
        item,
        {"notes": [{"beat": 0.0, "pitch": 36, "velocity": 100, "duration": 0.75},
                   {"beat": 0.5, "pitch": 36, "velocity": 90, "duration": 0.25}],
         "length_beats": 4},
        1, 1, 4,
    )
    assert phrase.collisions_clipped == 1
    first, second = phrase.notes
    assert first.pitch == second.pitch == 36
    assert first.end_s == pytest.approx(second.start_s)
    assert first.end_qn <= second.start_qn + 1e-9


def test_generate_humanize_is_bounded_and_deterministic():
    tempo = tempo_context()
    item = midi_item(item_context(length_s=4.0), tempo)
    pattern = {"notes": [{"beat": float(beat), "pitch": 36, "velocity": 100}
                         for beat in range(4)],
               "length_beats": 4}
    config = {"time_ms": 4.0, "velocity": 3, "seed": 1234}
    first = generate(item, pattern, 1, 1, 1, humanize=config)
    second = generate(item, pattern, 1, 1, 1, humanize=config)
    assert first == second

    clean = generate(item, pattern, 1, 1, 1)
    for shifted, straight in zip(first.notes, clean.notes):
        assert abs(shifted.start_s - straight.start_s) <= 0.004 + 1e-12
        assert abs(shifted.velocity - straight.velocity) <= 3

    report = grid_check(generate_item_with(item, first), 5.0, 1)
    assert report.notes_beyond_tolerance == 0, report.to_dict()
    assert report.max_abs_deviation_ms <= 4.0 + 1e-6


def test_generate_rejects_accents_for_missing_pattern_beats():
    item = midi_item(item_context(length_s=4.0), tempo_context())
    with pytest.raises(ValidationError, match="does not contain"):
        generate(
            item,
            {"notes": [{"beat": 0, "pitch": 36, "velocity": 100}],
             "length_beats": 4},
            1, 1, 1,
            accents={"beats": [2], "velocity_delta": 10},
        )


def test_generate_rejects_duplicate_onsets_from_pattern_data():
    item = midi_item(item_context(length_s=4.0), tempo_context())
    with pytest.raises(ValidationError, match="same pitch at one position"):
        generate(item, {"notes": [
            {"beat": 0.0, "pitch": 36, "velocity": 100, "duration": 0.5},
            {"beat": 0.25, "pitch": 36, "velocity": 90, "duration": 0.5},
            {"beat": 0.25, "pitch": 36, "velocity": 80, "duration": 0.5},
        ], "length_beats": 4}, 1, 1, 4)


# ---------------------------------------------------------------------------
# grid check
# ---------------------------------------------------------------------------

def test_grid_check_returns_numbers_without_a_verdict():
    item = midi_item(item_context(notes=[
        note_context(0.0, 0.25, 36, 100, start_ppq=0.0),
        note_context(1.017, 1.25, 38, 90, start_ppq=2.034),
    ]), tempo_context())
    report = grid_check(item, 10.0)
    payload = report.to_dict()
    assert "ok" not in payload
    assert all("on_grid" not in row for row in payload["deviations"])
    assert report.note_count == 2
    assert report.notes_within_tolerance == 1
    assert report.notes_beyond_tolerance == 1
    assert report.max_abs_deviation_ms == pytest.approx(17.0, abs=1e-6)
    off = [row for row in report.deviations if row["abs_deviation_ms"] > 10.0]
    assert off[0]["pitch"] == 38
    assert off[0]["deviation_ms"] == pytest.approx(17.0, abs=1e-6)
    assert off[0]["grid_bar"] == 1
    assert off[0]["grid_beat"] == pytest.approx(2.0)


def test_grid_check_uses_the_requested_subdivision():
    item = midi_item(item_context(notes=[
        note_context(0.125, 0.25, 42, 90, start_ppq=0.25),
    ]), tempo_context())
    assert grid_check(item, 1.0).notes_beyond_tolerance == 1
    assert grid_check(item, 1.0, subdivision=2).notes_beyond_tolerance == 1
    assert grid_check(item, 1.0, subdivision=4).notes_beyond_tolerance == 0


# ---------------------------------------------------------------------------
# replace
# ---------------------------------------------------------------------------

def test_replace_validates_out_of_take_notes():
    item = midi_item(item_context(position_s=4.0, length_s=4.0), tempo_context())
    with pytest.raises(ValidationError, match="outside the item's take"):
        replace_notes(item, [{"start_s": 3.0, "end_s": 3.5,
                              "pitch": 36, "velocity": 100}])


def test_replace_allows_hidden_notes_inside_an_offset_take():
    item = midi_item(
        item_context(position_s=4.0, length_s=4.0, start_offset_s=1.0),
        tempo_context(),
    )
    payload = replace_notes(item, [{"start_s": 3.5, "end_s": 3.75,
                                    "pitch": 50, "velocity": 80}])
    assert payload == [{"time_s": 3.5, "end_s": 3.75, "pitch": 50,
                        "velocity": 80}]


def test_replace_rejects_duplicate_onsets_and_overlaps():
    item = midi_item(item_context(length_s=4.0), tempo_context())
    with pytest.raises(ValidationError, match="collide") as duplicate:
        replace_notes(item, [{"start_s": 1.0, "end_s": 1.5, "pitch": 36,
                              "velocity": 100},
                             {"start_s": 1.0, "end_s": 1.25, "pitch": 36,
                              "velocity": 90}])
    assert duplicate.value.details["reason"] == "duplicate_onset"

    with pytest.raises(ValidationError, match="collide") as overlap:
        replace_notes(item, [{"start_s": 1.0, "end_s": 2.0, "pitch": 36,
                              "velocity": 100},
                             {"start_s": 1.5, "end_s": 1.75, "pitch": 36,
                              "velocity": 90}])
    assert overlap.value.details["reason"] == "overlapping_same_pitch"


def test_replace_rejects_bad_fields():
    item = midi_item(item_context(length_s=4.0), tempo_context())
    with pytest.raises(ValidationError, match="velocity"):
        replace_notes(item, [{"start_s": 1.0, "end_s": 1.5, "pitch": 36,
                              "velocity": 0}])
    with pytest.raises(ValidationError, match="Unknown MIDI note field"):
        replace_notes(item, [{"start_s": 1.0, "end_s": 1.5, "pitch": 36,
                              "velocity": 100, "colour": "red"}])


def test_service_replace_sends_absolute_seconds_and_proves_counts():
    context = project_context(
        item_context(position_s=4.0, length_s=4.0),
        tempo_context(),
    )
    bridge = FakeBridge()
    service = MidiService(FakeSession(context, bridge))
    result = service.replace("item-1", [{"start_s": 4.5, "end_s": 4.75,
                                         "pitch": 36, "velocity": 100}])
    assert result["inserted"] == 1
    assert result["removed"] == 3
    operation, payload = bridge.calls[0]
    assert operation == "midi.replace"
    assert payload["item_guid"] == "item-1"
    assert payload["notes"][0]["time_s"] == pytest.approx(4.5)


# ---------------------------------------------------------------------------
# service generate / preview / statistics
# ---------------------------------------------------------------------------

def test_service_generate_apply_keeps_notes_outside_the_bars():
    context = project_context(
        item_context(length_s=12.0, notes=[
            note_context(0.5, 0.75, 50, 70),
            note_context(10.0, 10.25, 50, 70),
        ]),
        tempo_context(),
    )
    bridge = FakeBridge()
    service = MidiService(FakeSession(context, bridge))
    result = service.generate(
        "item-1",
        {"notes": [{"beat": 0, "pitch": 36, "velocity": 100}],
         "length_beats": 4},
        1, 1, 1, apply=True,
    )
    assert result["kept_existing"] == 1
    assert result["merged_note_count"] == 2
    _, payload = bridge.calls[0]
    times = [note["time_s"] for note in payload["notes"]]
    assert times == [0.0, 10.0]


def test_service_preview_uses_injected_renderer_and_bar_range():
    context = project_context(item_context(length_s=12.0), tempo_context())
    renderer = RecordingRenderer({"path": "fragment.wav"})
    service = MidiService(FakeSession(context), renderer=renderer)
    result = service.preview("item-1", [2, 3])
    assert result["start_s"] == pytest.approx(2.0)
    assert result["end_s"] == pytest.approx(6.0)
    assert result["duration_s"] == pytest.approx(4.0)
    assert result["render"] == {"path": "fragment.wav"}
    assert renderer.calls == [{"item_guid": "item-1", "start_s": 2.0,
                               "end_s": 6.0,
                               "bar_range": {"from_bar": 2, "to_bar": 3},
                               "proposed_notes": None}]


def test_service_preview_passes_a_candidate_phrase_to_the_renderer():
    context = project_context(item_context(length_s=12.0), tempo_context())
    renderer = RecordingRenderer({"path": "candidate.wav"})
    service = MidiService(FakeSession(context), renderer=renderer)
    result = service.preview(
        "item-1", [1, 1],
        notes=[{"start_s": 0.0, "end_s": 0.25, "pitch": 36, "velocity": 100}],
    )
    assert result["proposed_note_count"] == 1
    assert renderer.calls[0]["proposed_notes"] == [
        {"time_s": 0.0, "end_s": 0.25, "pitch": 36, "velocity": 100}]


def test_service_preview_needs_a_renderer():
    context = project_context(item_context(length_s=12.0), tempo_context())
    service = MidiService(FakeSession(context))
    with pytest.raises(ValidationError, match="injected renderer"):
        service.preview("item-1", 1)


def test_service_statistics_keep_the_old_shape_and_skip_audio_items():
    midi = item_context(notes=[
        note_context(0.0, 0.25, 36, 100, start_ppq=0.0),
        note_context(0.5, 0.75, 36, 80, start_ppq=1.0),
        note_context(1.0, 1.25, 38, 90, start_ppq=2.0),
    ])
    audio = {"guid": "audio-1", "position_s": 0.0, "length_s": 2.0,
             "active_take": {"is_midi": False}}
    context = project_context(midi, tempo_context(), extra_items=[audio])
    service = MidiService(FakeSession(context))
    result = service.statistics()
    assert result["action"] == "notes"
    assert len(result["items"]) == 1
    row = result["items"][0]
    assert row["track_name"] == "drums"
    assert row["item_guid"] == "item-1"
    assert row["note_count"] == 3
    assert row["pitches"] == [
        {"pitch": 36, "notes": 2, "velocity_min": 80,
         "velocity_mean": 90.0, "velocity_max": 100},
        {"pitch": 38, "notes": 1, "velocity_min": 90,
         "velocity_mean": 90.0, "velocity_max": 90},
    ]


def test_item_statistics_returns_none_without_notes():
    item = midi_item(item_context(), tempo_context())
    assert item_statistics(item) is None
