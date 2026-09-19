"""Layered configuration for Trend Analyst (spec §10).

STUB — implemented in phase P0-T2.

Precedence, lowest to highest:

    defaults  <  config/settings.yaml  <  config/settings.<TA_ENV>.yaml
              <  config/secrets.local.yaml (untracked secrets, README D2/B4)
              <  environment (TA_*, optional overrides; what CI uses)
              <  CLI (--set section.key=value)

Secrets are `pydantic.SecretStr` and are redacted from logs, repr and JSON output.
"""

from __future__ import annotations

STUB_PHASE = "P0-T2"
