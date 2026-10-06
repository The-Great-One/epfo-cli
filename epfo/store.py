"""A local store of what the portal reported, so a run can be compared to the last.

The portal serves the current state and forgets nothing that matters, but it will
not tell you *what changed* — every read is a fresh snapshot with no history. This
module keeps the snapshots, which is what turns the CLI from a reader into
something you can run on a schedule and be told about.

Storage is SQLite in the same directory as the config (mode 0600, like the
config): the data is a record of the account holder's own contributions, so it is
no more public than the config that points at it.

The comparison is deliberately **additive**: a month that appears, or a month whose
figures moved, is reported. An observation that disappears is *also* reported,
because silently dropping a row that was there last time is exactly the kind of
change worth knowing about.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


def default_store_path() -> Path:
    """Where the store lives: beside the config, resolved on every call.

    Deliberately neither a module constant nor a default argument — both bind at
    import, so the operator's real store would leak into tests and ``EPFO_CLI_HOME``
    could not redirect it. (``config.CONFIG_PATH`` has that flaw; do not copy it.)
    """
    home = os.environ.get("EPFO_CLI_HOME") or Path.home() / ".epfo-cli"
    return Path(home) / "ledger.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contribution (
    member_id        TEXT NOT NULL,
    financial_year   TEXT NOT NULL,
    wage_month       TEXT NOT NULL,
    transaction_date TEXT,
    transaction_type TEXT,
    particulars      TEXT,
    epf_wages        REAL,
    eps_wages        REAL,
    employee_share   REAL,
    employer_share   REAL,
    pension_share    REAL,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    PRIMARY KEY (member_id, financial_year, wage_month, transaction_type)
);
CREATE INDEX IF NOT EXISTS contribution_member ON contribution(member_id);
"""

# Money is compared on the cents that the portal prints, not on an exact float.
# Two reads of the same row must not register as a change because of binary
# rounding.
_TOLERANCE = 0.005


@dataclass
class Change:
    """One difference between this observation and the stored one."""

    kind: str          # "new" | "changed" | "missing"
    member_id: str
    financial_year: str
    wage_month: str
    detail: str = ""


@dataclass
class Observation:
    """Every ledger row seen for one member in one financial year."""

    member_id: str
    financial_year: str
    rows: list


@dataclass
class Store:
    """The stored snapshots, and the diff of a fresh observation against them."""

    path: Path | None = None
    changes: list[Change] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = default_store_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        # The row holds amounts, member ids and months. Same reasoning as the
        # config file: 0600.
        self.path.chmod(0o600)
        return conn

    def rows_for(self, member_id: str) -> dict[tuple[str, str], sqlite3.Row]:
        """Stored rows keyed by (financial_year, wage_month)."""
        with self._connect() as conn:
            return {(r["financial_year"], r["wage_month"]): r
                    for r in conn.execute(
                        "SELECT * FROM contribution WHERE member_id = ?",
                        (member_id,))}

    def record(self, observations: list[Observation], seen_on: str,
               keep: int | None = None) -> list[Change]:
        """Store the observation and return what changed against what was held.

        ``seen_on`` is the timestamp written to ``first_seen``/``last_seen``; it
        is passed in so the caller (and its tests) own the clock.
        """
        self.changes = []
        with self._connect() as conn:
            for observation in observations:
                stored = {(r["financial_year"], r["wage_month"]): r
                          for r in conn.execute(
                              "SELECT * FROM contribution WHERE member_id = ?",
                              (observation.member_id,))}
                fresh: set[tuple[str, str]] = set()
                for row in observation.rows:
                    key = (observation.financial_year, row.wage_month)
                    fresh.add(key)
                    previous = stored.get(key)
                    if previous is None:
                        self.changes.append(Change(
                            "new", observation.member_id,
                            observation.financial_year, row.wage_month,
                            _describe(row)))
                    else:
                        moved = _differences(previous, row)
                        if moved:
                            self.changes.append(Change(
                                "changed", observation.member_id,
                                observation.financial_year, row.wage_month,
                                moved))
                    conn.execute(
                        "INSERT INTO contribution (member_id, financial_year, "
                        "wage_month, transaction_date, transaction_type, "
                        "particulars, epf_wages, eps_wages, employee_share, "
                        "employer_share, pension_share, first_seen, last_seen) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(member_id, financial_year, wage_month, "
                        "transaction_type) DO UPDATE SET "
                        "transaction_date=excluded.transaction_date, "
                        "particulars=excluded.particulars, "
                        "epf_wages=excluded.epf_wages, eps_wages=excluded.eps_wages, "
                        "employee_share=excluded.employee_share, "
                        "employer_share=excluded.employer_share, "
                        "pension_share=excluded.pension_share, "
                        "last_seen=excluded.last_seen",
                        (observation.member_id, observation.financial_year,
                         row.wage_month, row.transaction_date,
                         row.transaction_type, row.particulars, row.epf_wages,
                         row.eps_wages, row.employee_share, row.employer_share,
                         row.pension_share, seen_on, seen_on))

                # A row we held that this observation did not report. It is worth
                # surfacing rather than deleting quietly: the portal has been
                # known to serve a year one at a time, and a member with no
                # contributions in a year legitimately returns none.
                for key, previous in stored.items():
                    if key not in fresh:
                        self.changes.append(Change(
                            "missing", observation.member_id, key[0], key[1],
                            "was stored, not returned this run"))
        self._prune(keep)
        return self.changes

    def summary(self) -> dict[str, int]:
        """Stored row counts per member, for a run with no fresh observation."""
        with self._connect() as conn:
            return {r["member_id"]: r["n"] for r in conn.execute(
                "SELECT member_id, COUNT(*) AS n FROM contribution "
                "GROUP BY member_id ORDER BY member_id")}

    def years(self) -> list[str]:
        """The financial years held, newest first."""
        with self._connect() as conn:
            return [r["financial_year"] for r in conn.execute(
                "SELECT DISTINCT financial_year FROM contribution "
                "ORDER BY financial_year DESC")]

    def last_seen(self) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(last_seen) AS t FROM contribution").fetchone()
            return row["t"] or "" if row else ""

    def _prune(self, keep: int | None) -> None:
        """Keep only the ``keep`` most recent financial years, if asked.

        A member who has been contributing for a decade and runs this weekly
        otherwise accumulates a row per month forever. Nothing is lost that the
        portal cannot serve again.
        """
        if not keep or keep <= 0:
            return
        with self._connect() as conn:
            for year in self.years()[keep:]:
                conn.execute("DELETE FROM contribution WHERE financial_year = ?",
                             (year,))


def _describe(row) -> str:
    return (f"{row.transaction_date or '-'} "
            f"employee {(row.employee_share or 0):,.2f} "
            f"employer {(row.employer_share or 0):,.2f}")


def _differences(previous, row) -> str:
    """Human-readable description of what moved between two readings of a row."""
    fields = (("epf_wages", "epf wages"), ("employee_share", "employee"),
              ("employer_share", "employer"), ("pension_share", "pension"))
    moved = []
    for name, label in fields:
        was = previous[name] or 0.0
        now = getattr(row, name) or 0.0
        if abs(was - now) > _TOLERANCE:
            moved.append(f"{label} {was:,.2f} → {now:,.2f}")
    return ", ".join(moved)
