"""Operational scripts.

These are the entry points an operator or the monitor agent runs by hand (see AGENT.md):
provisioning the database, bringing it up and down, running the gate, and syncing the
source registry. They are a package so that tests can import and exercise them directly
instead of shelling out to them.
"""

from __future__ import annotations

__all__: list[str] = []
