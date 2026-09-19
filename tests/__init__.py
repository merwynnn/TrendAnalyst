"""Test package.

A real package (rather than a directory of loose modules) so tests can share fixtures
and constants — `tests.conftest` for fixtures, `tests.test_store.EXPECTED_TABLES` for the
schema shape that the migration tests assert against.
"""

from __future__ import annotations

__all__: list[str] = []
