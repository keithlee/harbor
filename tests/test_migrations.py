"""Guards for the checked-in SQL migrations and their statement splitter.

The phase migrations cannot use ``executescript`` because each needs to skip a
non-idempotent ALTER, so they split the script themselves.  Splitting on ``;``
before stripping comments silently corrupts any migration whose prose contains
a semicolon -- these tests pin the correct order.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harbor.db import sql_statements


MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql"))

SQL_KEYWORDS = ("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP", "PRAGMA")


def test_migrations_directory_is_not_empty() -> None:
    assert MIGRATIONS, "no migrations found to verify"


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda p: p.name)
def test_every_statement_starts_with_a_sql_keyword(migration: Path) -> None:
    """A comment fragment reaching SQLite is the exact Phase 9 regression."""

    statements = sql_statements(migration.read_text(encoding="utf-8"))
    assert statements, f"{migration.name} parsed to zero statements"
    for statement in statements:
        assert statement.upper().startswith(SQL_KEYWORDS), (
            f"{migration.name} produced a non-statement fragment: {statement!r}"
        )


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda p: p.name)
def test_every_statement_is_accepted_by_sqlite(migration: Path) -> None:
    """Parse each statement without running it, so DDL order cannot mask a typo."""

    connection = sqlite3.connect(":memory:")
    try:
        for statement in sql_statements(migration.read_text(encoding="utf-8")):
            # ``EXPLAIN`` compiles the statement and reports a syntax error
            # without requiring the referenced tables to exist yet.
            try:
                connection.execute(f"EXPLAIN {statement}")
            except sqlite3.OperationalError as exc:
                if "syntax error" in str(exc):
                    raise AssertionError(f"{migration.name}: {exc} in {statement!r}") from exc
                # "no such table" is expected: migrations are additive and
                # assume earlier phases created their tables.
    finally:
        connection.close()


def test_semicolon_inside_a_comment_is_not_a_statement_boundary() -> None:
    script = """
-- authorities for ticket and PR state; the runner records what it observed.
ALTER TABLE runs ADD COLUMN cleanup_at TEXT;
"""
    assert sql_statements(script) == ["ALTER TABLE runs ADD COLUMN cleanup_at TEXT"]


def test_trailing_comment_after_a_statement_is_dropped() -> None:
    script = "CREATE TABLE t (a TEXT); -- note; with a semicolon\n"
    assert sql_statements(script) == ["CREATE TABLE t (a TEXT)"]


def test_block_comment_containing_a_semicolon_is_dropped() -> None:
    script = "/* one; two */\nCREATE TABLE t (a TEXT);"
    assert sql_statements(script) == ["CREATE TABLE t (a TEXT)"]


def test_semicolon_inside_a_string_literal_is_preserved() -> None:
    script = "INSERT INTO t(a) VALUES ('one; two');"
    assert sql_statements(script) == ["INSERT INTO t(a) VALUES ('one; two')"]


def test_escaped_quote_inside_a_string_literal_is_preserved() -> None:
    script = "INSERT INTO t(a) VALUES ('it''s; fine');"
    assert sql_statements(script) == ["INSERT INTO t(a) VALUES ('it''s; fine')"]


def test_statement_without_a_trailing_semicolon_is_returned() -> None:
    assert sql_statements("CREATE TABLE t (a TEXT)") == ["CREATE TABLE t (a TEXT)"]
