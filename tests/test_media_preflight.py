"""Media preflight: REAPER's inactive-window offline state must self-heal."""

from __future__ import annotations

from reaper_audio.daw.media_preflight import ensure_media_online, unresolved_rows


def _context(online: bool) -> dict:
    return {
        "tracks": [{
            "guid": "{T}", "index": 0, "name": "VOX", "mute": False,
            "items": [{
                "guid": "{I}", "mute": False,
                "source_online": True if online else False,
                "source_length_s": 12.0 if online else 0,
                "source_sample_rate": 44100 if online else 0,
                "source_channels": 2 if online else 0,
                "active_take": {"is_midi": False, "source": "C:/x/vox.wav"},
            }],
        }]
    }


class _Bridge:
    def __init__(self) -> None:
        self.commands: list[tuple] = []

    def call_api(self, name: str, *values, **kwargs):
        self.commands.append((name, values))
        return type("Result", (), {"returns": []})()


class _Session:
    def __init__(self, become_online: bool) -> None:
        self.online = False
        self.become_online = become_online
        self.bridge = _Bridge()
        self.snapshots = 0

    def snapshot(self, include_fx_params: bool = False):
        self.snapshots += 1
        return _context(self.online)

    def snapshot_after_command(self) -> None:
        if self.become_online:
            self.online = True


def test_offline_rows_are_detected() -> None:
    rows = unresolved_rows(_context(False)["tracks"])
    assert rows and rows[0]["track_name"] == "VOX"
    assert unresolved_rows(_context(True)["tracks"]) == []


def test_preflight_repairs_and_proves_it(monkeypatch) -> None:
    session = _Session(become_online=True)
    monkeypatch.setattr("reaper_audio.daw.media_preflight.time.sleep",
                        lambda _s: session.snapshot_after_command())
    result = ensure_media_online(session, attempts=3, wait_s=0)
    assert result["ok"] is True and result["recovered"] is True
    assert session.bridge.commands and session.bridge.commands[0][0] == "Main_OnCommand"


def test_preflight_fails_closed_when_media_stays_offline(monkeypatch) -> None:
    session = _Session(become_online=False)
    monkeypatch.setattr("reaper_audio.daw.media_preflight.time.sleep", lambda _s: None)
    result = ensure_media_online(session, attempts=2, wait_s=0)
    assert result["ok"] is False and result["unresolved"]
