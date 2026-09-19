"""
Tests for replay/outputs.py — the near-miss collapse and the blocker summary.

The near-miss log is where the original failure hid. It recorded only bars with
exactly ONE failing gate, so on the run where rejection had blocked 11 of 13 live
alerts it reported rejection once — rejection and pullback fail together when a bar
has no wicks. These tests pin the completeness that replaced it.
"""

import csv
import os
from datetime import date

import pytest

import config
from replay.outputs import (ALERT_FIELDS, NEARMISS_FIELDS, blocker_summary,
                            collapse_nearmiss, write_alerts, write_nearmiss)
from replay.ticks import OHLC_MODE

pytestmark = pytest.mark.skipif(
    not config.DRY_RUN, reason="replay requires BN_DRY_RUN=1")


def _raw(bar="09:15", direction="long", setup="long", gate="volume",
         count=1, ts="2026-08-14T09:16:00"):
    return {"timestamp": ts, "bar_start": bar, "direction": direction,
            "setup": setup, "blocking_gate": gate, "blocker_count": count}


class _Result:
    def __init__(self, nearmiss, alerts=(), day=date(2026, 8, 14)):
        self.nearmiss, self.alerts, self.day = nearmiss, list(alerts), day
        self.tick_mode = OHLC_MODE


# ------------------------------------------------------------------ collapse

def test_repeated_snapshots_collapse_to_one_row_with_a_count():
    """Raw rows are change-keyed, so a flickering gate produces far more of them
    than one that blocks solidly. Counting raw rows biases the totals toward
    UNSTABLE near-misses."""
    rows = collapse_nearmiss([_raw(ts="2026-08-14T09:16:00"),
                              _raw(ts="2026-08-14T09:16:02"),
                              _raw(ts="2026-08-14T09:16:04")])
    assert len(rows) == 1
    assert rows[0]["snapshots"] == 3
    assert rows[0]["first_seen"] == "2026-08-14T09:16:00"
    assert rows[0]["last_seen"] == "2026-08-14T09:16:04"


def test_different_gates_on_one_bar_stay_separate():
    rows = collapse_nearmiss([_raw(gate="volume"), _raw(gate="rejection")])
    assert {r["blocking_gate"] for r in rows} == {"volume", "rejection"}


def test_the_same_gate_on_two_bars_stays_separate():
    rows = collapse_nearmiss([_raw(bar="09:15"), _raw(bar="09:20")])
    assert len(rows) == 2


def test_blocker_count_keeps_the_closest_the_setup_came_to_firing():
    rows = collapse_nearmiss([_raw(count=4), _raw(count=1), _raw(count=3)])
    assert rows[0]["blocker_count"] == 1


# ------------------------------------------------------------------- summary

def test_all_blockers_view_sees_a_correlated_gate_the_sole_view_misses():
    """The exact shape that hid the original bug: rejection never fails alone, so
    the single-blocker filter cannot see it at all."""
    nearmiss = []
    for bar in ("09:15", "09:20", "09:25"):
        nearmiss.append(_raw(bar=bar, gate="rejection", count=2))
        nearmiss.append(_raw(bar=bar, gate="pullback", count=2))
    nearmiss.append(_raw(bar="09:30", gate="volume", count=1))
    r = _Result(nearmiss)

    assert blocker_summary(r)["rejection"] == 3
    assert blocker_summary(r, sole_blocker_only=True)["rejection"] == 0
    assert blocker_summary(r, sole_blocker_only=True)["volume"] == 1


def test_summary_keys_on_setup_so_it_does_not_saturate():
    """Keying on (bar, gate) alone saturates at the bar count: every bar evaluates a
    long and a short setup, at most one of which can be valid, so trend blocks the
    other on essentially every bar. Measured on 14 Aug that put four gates at
    exactly 76 — the bar count — and ranked nothing."""
    nearmiss = [_raw(direction="long", setup="long", gate="trend"),
                _raw(direction="short", setup="short", gate="trend")]
    assert blocker_summary(_Result(nearmiss))["trend"] == 2
    assert blocker_summary(_Result(nearmiss), direction="long")["trend"] == 1


# --------------------------------------------------------------------- files

def test_both_files_carry_engine_version_and_tick_mode(tmp_path):
    r = _Result([_raw()])
    for path, fields in ((write_nearmiss(str(tmp_path), r), NEARMISS_FIELDS),
                         (write_alerts(str(tmp_path), r), ALERT_FIELDS)):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == fields
            for row in reader:
                assert row["engine_version"] and row["tick_mode"] == OHLC_MODE


def test_filenames_pin_both_version_and_mode(tmp_path):
    """A file naming only the day would let a tick-model change read as an
    unexplained diff."""
    name = os.path.basename(write_nearmiss(str(tmp_path), _Result([_raw()])))
    assert "2026-08-14" in name and OHLC_MODE in name
    assert any(part for part in name.split("_") if len(part) == 8)


def test_alert_columns_match_the_live_log_plus_tick_mode():
    """So replay output diffs against alert_log.csv without a mapping step.

    Resolves the live log rather than hardcoding vm-csv/: that directory is the
    workstation's pulled copy and does not exist on the VM, where the same log is
    csv/. Hardcoded, this failed on the VM only — the worst place to find out."""
    from replay.compare import LIVE_DIR
    with open(os.path.join(LIVE_DIR, "alert_log.csv"), encoding="utf-8") as f:
        live_cols = next(csv.reader(f))
    assert ALERT_FIELDS == live_cols + ["tick_mode"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
