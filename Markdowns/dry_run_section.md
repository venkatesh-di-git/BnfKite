# Dry-run mode — section to add to `bn_linux_migration.md`

Insert as a new section after §6 (Daily routine). Rationale for the addition is at the
bottom; it is not meant to go into the doc.

**Re-checked against the code 14 Aug 2026.** Two of the original implementation points
turned out to be already satisfied, and the `engine_version()` consequence was backwards.
Corrected below — the change is now roughly six lines.

---

## N. Dry-run mode — testing locally while the VM session is live

The VM is the live engine. Section 6 forbids running `app.py` locally at the same time:
two websocket feeds, two `LiveFiveMinuteSession`s, two Telegram streams, two divergent
CSV sets. The last of those is the real cost — the replay engine exists to compare
against alert history, and a split corpus is worse than no corpus.

Dry-run mode removes the collision instead of the second process. A local run observes
and computes normally, but cannot emit to Telegram and cannot write into the shared
`csv/`.

```bash
BN_DRY_RUN=1 python app.py
```

### What it gates

| Behaviour | Normal | `BN_DRY_RUN=1` |
|---|---|---|
| Telegram send | sends | logs the payload, sends nothing |
| CSV output dir | `csv/` | `csv-dryrun/` |
| `run_manifest.csv` row | written to `csv/` | written to `csv-dryrun/` |
| `latest_volume_profile.json` | repo root | `csv-dryrun/` |
| Websocket feed | live | live (unchanged) |
| Signal/decision logic | unchanged | unchanged |

The feed and the engines are deliberately *not* gated. A dry run that skips them tests
nothing. Only the two outputs that leave the process are redirected.

The invariant worth stating, because it is directly testable and because it makes cleanup
a single `rm -rf`:

> **A dry run writes nothing outside `csv-dryrun/`, and sends nothing.**

`latest_volume_profile.json` is the row that makes this non-trivial. It is the one output
that does *not* live under `CSV_DIR` — it sits at the repo root — and it is the file
`healthcheck.py` stats for freshness. Redirect `CSV_DIR` alone and a dry run silently
overwrites the live heartbeat.

### Design constraints

**Default off.** Unset means normal. The VM never sets it, so a missing env var can never
silently disable live alerting — the failure mode is loud (alerts fire) rather than quiet
(alerts vanish).

**Read once, at startup.** Not per-call. A mode that can change mid-session is a mode you
cannot reason about afterwards when reading the logs.

**Visible — on the recurring line, not just at startup.** The one thing worse than
duplicate alerts is sitting in front of a dry run at 09:20 believing it is live.

A startup banner is not enough for `main.py`, which is what the VM actually runs: one line
printed once scrolls out of `journalctl -f` within seconds, and the log you read at 09:20
is the tail, not the head. Put the mode on the **iteration line** — the one that already
prints LTP, VWAP and bar count — so it cannot be missed at any point in the session. Only
when the flag is set, so live output keeps its existing format:

```python
tag = " DRY" if config.DRY_RUN else ""
print(f"[{now:%H:%M:%S}]{tag} {contract.tradingsymbol}  LTP=... ")
```

`app.py`'s badge is the easy half and matters less — it is already persistent on screen,
and the VM does not run it.

**One flag, not two.** Resist a separate `BN_NO_TELEGRAM` and `BN_CSV_DIR`. Two flags mean
four states, two of which are wrong (Telegram live but CSVs redirected, and vice versa).

**`csv-dryrun/` is disposable and not synced.** It does not match `*.py`, so `sync.sh`
already excludes it. Add it to `.gitignore` alongside `csv/`.

### Implementation points

Both of the original points are **already satisfied**, which is why this is now a small
change rather than a refactor:

- The Telegram guard exists at the single outbound call site — `alert_engine.py`,
  `_send_telegram()` returns early on `self.enable_telegram`. And `SessionRunner.__init__`
  already accepts an injectable engine (`alert_engine: Optional[AlertEngine] = None`), so
  the flag needs wiring, not writing.
- `CSV_DIR` is already the single constant. `LOG_FILE`, `SIGNAL_LOG_FILE` and
  `ALERT_LOG_FILE` all derive from it in `config.py`, and `run_manifest.csv` derives from
  the signal-log path inside `SignalStateLog`. The redirect is one line.

What actually remains:

- **`config.py`** — read `BN_DRY_RUN` once at import; point `CSV_DIR` at `csv-dryrun/`;
  move `OUTPUT_FILE` inside `CSV_DIR`. The other three paths follow. `write_output`,
  `AlertEngine._store` and `SignalStateLog` all resolve from `config` at call or
  construction time, so nothing else needs touching.

  **Order matters:** the existing `os.makedirs(CSV_DIR, exist_ok=True)` must stay
  *after* the redirect, or `csv-dryrun/` never gets created and the first dry run dies at
  09:15 on a missing directory — `write_output` and `SignalStateLog` both open by path and
  neither creates its parent. The directory is made at import for exactly this reason, so
  the redirect has to happen above it, not below.
- **`session_runner.py`** — one line: `AlertEngine(enable_telegram=not config.DRY_RUN)`.
- **`main.py` / `app.py`** — the visibility requirement above: the mode goes on `main.py`'s
  **iteration** line, not only its startup banner. `app.py` already has an `INSTANCE`
  badge; fold the mode into it and force the amber styling so a dry run can never read as
  the live instance.
- **`.gitignore`** — add `csv-dryrun/`.

### Consequence for `engine_version()` — none, and that is the test

The original draft of this section said the hash **would** change and needed re-baselining
on both machines. That was written on the assumption the Telegram guard had to be added.
It already exists, so none of the three hashed modules — `signal_engine.py`,
`decision_engine.py`, `alert_engine.py` — is touched. The wiring lands in
`session_runner.py` and `config.py`, neither of which is hashed.

So `engine_version()` **must not move**. Record it before and after:

```bash
python -c "import engine; print(engine.engine_version())"   # b3620733
```

A changed hash here does not mean "re-baseline" — it means the edit went into
`alert_engine.py` when it should not have. That makes the fingerprint a free correctness
check on where the change landed, which is a better outcome than the re-baseline the
original draft planned for.

### The inverse risk, and how far the guard actually reaches

The dangerous failure is not a local run polluting the corpus — it is `BN_DRY_RUN` set on
the **VM** by accident, so alerts stop silently. The mode on the iteration line makes it
visible to anyone looking. More usefully, nobody has to look: `healthcheck.py` stats
`config.OUTPUT_FILE`, which under a dry run moves into `csv-dryrun/`, so the real
`latest_volume_profile.json` goes stale and the probe Telegrams "engine stalled" within
five minutes.

That is the "loud rather than quiet" requirement satisfied by a mechanism that already
exists — but only because `OUTPUT_FILE` is redirected too. Redirect `CSV_DIR` alone and
the heartbeat keeps being written by the dry run, which is precisely the 10 Aug failure
shape: every check green, nothing being delivered.

**The guard has a hole, and it is worth closing in code rather than in prose.** It works
only because `healthcheck.py` runs as a *separate systemd unit* (`kite-healthcheck.timer`)
that does not inherit the scanner's environment. Set `BN_DRY_RUN` somewhere that reaches
both — `~/.profile`, `~/.config/environment.d/`, or a drop-in applied to both units — and
the probe stats the very file the dry run is refreshing and stays green forever. The
protection depends on a process boundary nobody has written down, and "I added the flag to
the service" is precisely the accident being guarded against.

Close it by making the probe immune to the flag rather than merely unaware of it:

```python
# config.py — always the real path, whatever BN_DRY_RUN says.
LIVE_OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "latest_volume_profile.json")
```

and have `healthcheck.py` stat `config.LIVE_OUTPUT_FILE` instead of `config.OUTPUT_FILE`.
Its job is to assert *the live engine is producing*; a dry run on the VM is never
legitimate, so there is no case where the probe should be satisfied by dry-run output. One
constant and one word, and the guard no longer depends on how the environment happens to
be arranged.

### Tests

The invariant is only worth stating if something enforces it. Four cases, in
`test_dry_run.py`:

**The invariant itself, end to end.** Snapshot `(mtime, size)` for every file under `csv/`
plus the root `latest_volume_profile.json`. In a **subprocess** with `BN_DRY_RUN=1` —
subprocess because the flag is read at import and the real startup path is the thing under
test — build a `SessionRunner` with the stubbed feed and bars from
`test_session_runner.py`, run `ensure_started` and one `tick`, exit. Re-snapshot and assert
nothing changed, and that `csv-dryrun/` gained rows. This is the test that would have
caught redirecting `CSV_DIR` while leaving `OUTPUT_FILE` at the root.

**Path resolution, both modes.** Same subprocess trick, printing `CSV_DIR`, `OUTPUT_FILE`,
`LOG_FILE`, `SIGNAL_LOG_FILE`, `ALERT_LOG_FILE`. Under the flag all five sit inside
`csv-dryrun/`; without it, four sit under `csv/` and `OUTPUT_FILE` at the root.

**Telegram wiring.** Monkeypatch `config.DRY_RUN`, construct a `SessionRunner`, assert
`alert_engine.enable_telegram` is `False`, and `True` in the converse case. The default
argument is evaluated inside `__init__`, so monkeypatching works without a subprocess.

**Flag truthiness.** `""`, `0`, `false`, `no` are off; `1`, `true`, `yes` are on. Cheap,
and the alternative is discovering at 09:15 that `BN_DRY_RUN=0` meant on.

Reuse the `_isolate_logs` fixture pattern from `test_session_runner.py` for anything that
does not need the subprocess, so no test ever resolves a real path by accident.

---

## Why this is worth the change (not for the doc)

The alternative to dry-run mode is stop-service → test → sync → restart, which costs the
measured ~76s alert gap while `_ema_history` refills, twice per test. That gap during
market hours is the thing this avoids.

It also makes "can I run this locally right now?" a question with a fixed answer instead
of one that depends on remembering what the VM is currently doing.
