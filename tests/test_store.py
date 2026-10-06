"""The local store: it must report real changes and only real changes.

The store exists to answer "what's new since last time", so the tests are about
the *diff*, not the schema. Two failure modes matter and both are pinned here: a
re-read of unchanged data must report nothing (or a scheduled job alerts every
run), and a row that disappears must be reported rather than silently dropped.
"""

from __future__ import annotations

from types import SimpleNamespace

from epfo.store import Observation, Store


def _row(month, employee=3600.0, employer=3600.0, wages=30000.0):
    return SimpleNamespace(
        wage_month=month, transaction_date="01-06-2026",
        transaction_type="Contribution", particulars="Contribution",
        epf_wages=wages, eps_wages=wages, employee_share=employee,
        employer_share=employer, pension_share=0.0)


def _store(tmp_path):
    return Store(path=tmp_path / "ledger.sqlite")


def test_first_run_reports_every_row_as_new(tmp_path):
    store = _store(tmp_path)
    changes = store.record(
        [Observation("M1", "2026 - 2027", [_row("May-2026"), _row("Jun-2026")])],
        seen_on="2026-10-06T10:00:00+05:30")
    assert [c.kind for c in changes] == ["new", "new"]
    assert [c.wage_month for c in changes] == ["May-2026", "Jun-2026"]


def test_a_second_identical_read_reports_nothing(tmp_path):
    # The single most important property: a scheduled run over unchanged data
    # must be silent, or every run alerts.
    store = _store(tmp_path)
    observation = [Observation("M1", "2026 - 2027", [_row("May-2026")])]
    store.record(observation, seen_on="2026-10-06T10:00:00+05:30")
    assert store.record(observation, seen_on="2026-10-07T10:00:00+05:30") == []


def test_a_new_month_is_reported_as_new(tmp_path):
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026")])],
                 seen_on="2026-10-06T10:00:00+05:30")
    changes = store.record(
        [Observation("M1", "2026 - 2027", [_row("May-2026"), _row("Jun-2026")])],
        seen_on="2026-10-07T10:00:00+05:30")
    assert [(c.kind, c.wage_month) for c in changes] == [("new", "Jun-2026")]


def test_a_corrected_figure_is_reported_with_both_values(tmp_path):
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026", employee=3600.0)])],
                 seen_on="2026-10-06T10:00:00+05:30")
    changes = store.record(
        [Observation("M1", "2026 - 2027", [_row("May-2026", employee=4500.0)])],
        seen_on="2026-10-07T10:00:00+05:30")
    assert len(changes) == 1
    assert changes[0].kind == "changed"
    assert "3,600.00" in changes[0].detail and "4,500.00" in changes[0].detail


def test_a_vanished_row_is_reported_not_dropped(tmp_path):
    # The portal has served a year one member at a time; a row that was there
    # and now is not is exactly the change worth surfacing.
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026"), _row("Jun-2026")])],
                 seen_on="2026-10-06T10:00:00+05:30")
    changes = store.record(
        [Observation("M1", "2026 - 2027", [_row("May-2026")])],
        seen_on="2026-10-07T10:00:00+05:30")
    assert [(c.kind, c.wage_month) for c in changes] == [("missing", "Jun-2026")]


def test_floating_point_noise_is_not_a_change(tmp_path):
    # 0.1 + 0.2 style drift must not register; the portal prints paise.
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026", employee=3600.0)])],
                 seen_on="2026-10-06T10:00:00+05:30")
    changes = store.record(
        [Observation("M1", "2026 - 2027", [_row("May-2026", employee=3600.001)])],
        seen_on="2026-10-07T10:00:00+05:30")
    assert changes == []


def test_members_do_not_contaminate_each_other(tmp_path):
    # Each member id is its own ledger; the same month in two members is not one
    # row being corrected.
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026")])],
                 seen_on="2026-10-06T10:00:00+05:30")
    changes = store.record(
        [Observation("M2", "2026 - 2027", [_row("May-2026", employee=9999.0)])],
        seen_on="2026-10-06T10:00:00+05:30")
    assert [c.kind for c in changes] == ["new"]


def test_summary_counts_stored_rows_per_member(tmp_path):
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026"), _row("Jun-2026")]),
                  Observation("M2", "2026 - 2027", [_row("May-2026")])],
                 seen_on="2026-10-06T10:00:00+05:30")
    assert store.summary() == {"M1": 2, "M2": 1}


def test_last_seen_records_the_run(tmp_path):
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026")])],
                 seen_on="2026-10-06T10:00:00+05:30")
    assert store.last_seen() == "2026-10-06T10:00:00+05:30"


def test_keep_prunes_older_years(tmp_path):
    store = _store(tmp_path)
    for year in ("2024 - 2025", "2025 - 2026", "2026 - 2027"):
        store.record([Observation("M1", year, [_row("May-2026")])],
                     seen_on="2026-10-06T10:00:00+05:30")
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026")])],
                 seen_on="2026-10-06T10:00:00+05:30", keep=1)
    assert list(store.summary()) == ["M1"]
    from epfo.store import Store as _S
    with _S(path=store.path)._connect() as conn:
        years = {r["financial_year"] for r in conn.execute(
            "SELECT financial_year FROM contribution")}
    assert years == {"2026 - 2027"}


def test_store_defaults_to_the_config_directory(tmp_path, monkeypatch):
    # The real store must never be written by a test: the default is resolved at
    # call time so EPFO_CLI_HOME can redirect it.
    monkeypatch.setenv("EPFO_CLI_HOME", str(tmp_path))
    import importlib
    from epfo import store as store_module
    importlib.reload(store_module)
    try:
        assert store_module.default_store_path() == tmp_path / "ledger.sqlite"
    finally:
        importlib.reload(store_module)


def test_the_store_file_is_not_world_readable(tmp_path):
    # It holds the account holder's contribution figures.
    import stat
    store = _store(tmp_path)
    store.record([Observation("M1", "2026 - 2027", [_row("May-2026")])],
                 seen_on="2026-10-06T10:00:00+05:30")
    mode = store.path.stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)
