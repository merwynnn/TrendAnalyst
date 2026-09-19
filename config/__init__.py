"""Trend Analyst configuration package.

Spec §3 places the settings module inside ``config/`` rather than under
``src/trend_analyst/``. To let typed code import it as a package (decision B2a,
README D2) this directory is a real package, and ``pyproject.toml`` ships it in the
wheel alongside ``trend_analyst``.

Contents:
    settings.py           the layered settings model + loader (spec §10)
    cli.py                ``python -m config.cli show`` — resolved config, secrets redacted
    settings.yaml         committed defaults
    settings.<env>.yaml   per-environment diffs (dev, prod)
    sources.yaml          the source registry; file order is execution order (spec §4.2)
    categories.yaml       category taxonomy (spec §6.1)
    secrets.local.yaml    UNTRACKED credentials; template: secrets.example.yaml
"""

from __future__ import annotations

__all__: list[str] = []
