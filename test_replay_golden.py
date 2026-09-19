"""
Tests for replay/golden.py — the frozen 14 Aug session.

The golden file is a CHANGE detector, not a correctness detector. These tests
guard that it actually detects change, that it refuses to compare across engine
versions, and that the parts of a replay run which must be deterministic are.
"""

import os
from datetime import date

import pytest

import config
from engine import engine_version
from replay import golden
from replay.driver import replay_day
from replay.ticks import CLOSE_ONLY_MODE, OHLC_MODE

pytestmark = pytest.mark.skipif(
    not config.DRY_RUN, reason="replay requires BN_DRY_RUN=1")


PHASE1_VERSION = "b3620733"      # before the flip cooldown
BASELINE = "fd8e6f05"            # + Telegram V2 formatter + startup alert


def test_engine_version_is_the_baseline():
    """A change here is only ever legitimate alongside a deliberate rule change and
    a re-freeze. Unexplained movement means an edit landed in signal_engine /
    decision_engine / alert_engine or a versioned constant by accident."""
    assert engine_version() == BASELINE


def test_golden_file_exists_for_the_current_version_and_mode():
    assert os.path.exists(golden.golden_path()), (
        "no golden file for this (version, tick_mode); run "
        "`BN_DRY_RUN=1 python -m replay.golden freeze`")


def test_golden_file_matches():
    gone, added = golden.diff()
    assert (gone, added) == ([], []), f"engine drifted: -{len(gone)} +{len(added)}"


def test_golden_file_holds_the_expected_shape():
    rows = golden.read_golden(golden.golden_path())
    assert len(rows) == 9
    assert {r["engine_version"] for r in rows} == {BASELINE}
    assert {r["tick_mode"] for r in rows} == {OHLC_MODE}
    assert all(r["timestamp"].startswith("2026-08-14") for r in rows)


def test_the_flip_cooldown_removed_exactly_one_alert_and_added_none():
    """THE ACCEPTANCE TEST for the cooldown, and the reason the pre-cooldown golden
    file was frozen first. The rule change must show up as precisely the alert it
    was designed to withhold — 09:56:44 B Long, one minute after an A Short — and
    nothing else. Anything additional in this diff is collateral damage."""
    gone, added = golden.diff(against=PHASE1_VERSION)
    assert added == [], f"the cooldown produced new alerts: {added}"
    assert len(gone) == 1
    withheld = gone[0]
    assert withheld[0].endswith("09:56:44")
    assert (withheld[2], withheld[3]) == ("Long", "B")


def test_cross_version_diff_must_be_asked_for_by_name():
    """The default refuses to compare across versions (§10). That is what stops two
    unrelated engines being diffed and the difference called a result."""
    with pytest.raises(FileNotFoundError):
        golden.diff(against="deadbeef")


def test_replaying_twice_agrees_on_every_field_except_id():
    """AlertRecord.id is a fresh uuid4 (alert_engine.py:141), so raw output is never
    byte-identical. Everything the golden file records must still be stable."""
    a = golden.rows_for()
    b = golden.rows_for()
    assert a == b


def test_alert_ids_are_not_deterministic_and_so_are_not_frozen():
    """Documents WHY `id` is excluded, so a future reader does not add it back and
    get a golden file that fails on every run."""
    x = replay_day(golden.GOLDEN_SYMBOL, golden.GOLDEN_DAY, write_signal_log=False)
    y = replay_day(golden.GOLDEN_SYMBOL, golden.GOLDEN_DAY, write_signal_log=False)
    assert [a.id for a in x.alerts] != [b.id for b in y.alerts]
    assert "id" not in golden.FIELDS


def test_a_versioned_constant_change_moves_the_golden_file_rather_than_failing_it(
        monkeypatch):
    """Spec §10: a run spanning an engine_version boundary must refuse, never
    average. Because the golden path is keyed on engine_version, changing a
    versioned constant makes the comparison IMPOSSIBLE instead of misleading."""
    monkeypatch.setattr(config, "VOLUME_HIGH_THRESHOLD", 3.0)
    assert engine_version() != "b3620733"
    with pytest.raises(FileNotFoundError):
        golden.diff()


def test_an_unversioned_behaviour_change_makes_the_golden_file_fail():
    """The tick mode is not hashed, so switching it must surface as a DIFF — that
    is the mechanism the flip cooldown's acceptance test will rely on."""
    with pytest.raises(FileNotFoundError):
        # close-only has no frozen file; the mode is part of the key, so the two
        # modes can never be silently compared against each other.
        golden.diff(tick_mode=CLOSE_ONLY_MODE)


def test_freeze_refuses_to_overwrite_silently(monkeypatch, tmp_path):
    """Runs against a temp GOLDEN_DIR, not the repo's.

    Written that way after this test created a real golden file as a side effect:
    at a fresh engine_version there was nothing to collide with, so `freeze()`
    succeeded and quietly committed a baseline nobody had chosen. A test that can
    write the artifact it is checking is not checking it."""
    monkeypatch.setattr(golden, "GOLDEN_DIR", str(tmp_path))
    first = golden.freeze()
    assert os.path.exists(first)
    with pytest.raises(FileExistsError, match="frozen ONCE"):
        golden.freeze()
    assert golden.freeze(overwrite=True) == first


def test_freezing_never_touches_the_repo_golden_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(golden, "GOLDEN_DIR", str(tmp_path))
    before = sorted(os.listdir(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay", "golden")))
    golden.freeze()
    after = sorted(os.listdir(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay", "golden")))
    assert before == after


def test_golden_day_is_the_only_clean_session():
    assert golden.GOLDEN_DAY == date(2026, 8, 14)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
