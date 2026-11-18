"""Stage B3: structural tokens — cheap authorisation for structural edits."""

from __future__ import annotations

import pytest

from reaper_audio.context import structural_token as st
from reaper_audio.errors import StaleContext, ValidationError

PROJECT = "reaper:{P}:C:/x/REAPER-CONTEXT-V2-TEST.RPP"


class FakeSession:
    def __init__(self, *, map_sha: str = "a" * 64, change_count: int = 5,
                 epoch: int = 1, instance: str = "inst-1"):
        self.map_sha = map_sha
        self.change_count = change_count
        self.epoch = epoch
        self.instance = instance

    def digest(self, project_id=None):
        return {"digest_version": 1, "change_count": self.change_count,
                "coverage": {}, "missing": [], "fields": {}}

    def snapshot(self, **kwargs):
        return {"revision": {"project_id": PROJECT, "change_count": self.change_count,
                             "epoch": self.epoch, "instance_id": self.instance}}


@pytest.fixture(autouse=True)
def _stable_fingerprint(monkeypatch):
    monkeypatch.setattr(st, "digest_evidence", lambda project_id, digest: type(
        "Evidence", (), {"fingerprint": "f" * 64})())


def test_mint_returns_a_token_bound_to_the_map() -> None:
    minted = st.mint(FakeSession(), PROJECT)
    assert minted["structural_token"].startswith("st_")
    assert minted["fingerprint"]["change_count"] == 5
    assert minted["fingerprint"]["map_sha256"] == "f" * 64


def test_valid_token_is_accepted() -> None:
    session = FakeSession()
    token = st.mint(session, PROJECT)["structural_token"]
    assert st.require(session, PROJECT, token)["change_count"] == 5


def test_token_from_another_project_is_refused() -> None:
    token = st.mint(FakeSession(), PROJECT)["structural_token"]
    with pytest.raises(StaleContext):
        st.require(FakeSession(), PROJECT + ".other", token)


def test_forged_or_unknown_token_is_refused() -> None:
    with pytest.raises(StaleContext):
        st.require(FakeSession(), PROJECT, "st_deadbeef")
    with pytest.raises(ValidationError):
        st.require(FakeSession(), PROJECT, "")


def test_token_dies_when_the_project_moves() -> None:
    session = FakeSession()
    token = st.mint(session, PROJECT)["structural_token"]
    session.change_count = 6
    with pytest.raises(StaleContext) as error:
        st.require(session, PROJECT, token)
    assert "revision moved" in str(error.value) or "map changed" in str(error.value)


def test_token_dies_when_the_instance_changes() -> None:
    session = FakeSession()
    token = st.mint(session, PROJECT)["structural_token"]
    session.instance = "inst-2"
    with pytest.raises(StaleContext):
        st.require(session, PROJECT, token)


def test_age_is_reported_for_diagnostics() -> None:
    token = st.mint(FakeSession(), PROJECT)["structural_token"]
    age = st.seconds_since_mint(token)
    assert age is not None and age >= 0
    assert st.seconds_since_mint("st_unknown") is None
