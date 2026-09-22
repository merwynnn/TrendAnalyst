"""Layered configuration for Trend Analyst (spec §10).

Precedence, lowest to highest::

    field defaults
      < config/settings.yaml                 committed, documented defaults
      < config/settings.<TA_ENV>.yaml        per-environment overrides
      < config/secrets.local.yaml            UNTRACKED — decision B4: keys live in a file
      < config/.env                           optional env-file overrides
      < environment TA_*                     optional; this is what CI uses
      < CLI  --set section.key=value         highest; what an operator types

Two invariants make this safe to rely on:

* every section forbids unknown keys (``extra="forbid"``), so a typo in YAML is a
  startup error instead of a silently ignored setting;
* every model is frozen — configuration is not state, and nothing downstream may
  mutate it in place. Secrets are ``SecretStr``: they cannot leak through ``repr``,
  logs or JSON dumps, and :meth:`Settings.secret_status` reports *whether* a key is
  configured without ever exposing it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

__all__ = [
    "AppSection",
    "BudgetsSection",
    "ConfigError",
    "DatabaseSection",
    "GateSection",
    "GatesSection",
    "HealthSection",
    "LLMSection",
    "RetentionSection",
    "Settings",
    "SourceInfo",
    "TierASection",
    "default_config_dir",
    "describe_sources",
    "load_settings",
    "parse_overrides",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent
ENV_NAME_VAR = "TA_ENV"
DEFAULT_ENV_NAME = "dev"

_SECTION_CONFIG = ConfigDict(extra="forbid", frozen=True)


class ConfigError(RuntimeError):
    """Raised when the configuration cannot be loaded (bad YAML, missing directory).

    Configuration problems must stop a run loudly: a pipeline that silently falls back
    to defaults would scan the wrong sources with the wrong budgets.
    """


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class AppSection(BaseModel):
    """Process-level settings."""

    model_config = _SECTION_CONFIG

    env: Literal["dev", "prod"] = "dev"
    log_level: LogLevel = "INFO"
    log_json: bool = True
    dry_run: bool = False
    timezone: str = "UTC"
    config_dir: Path = DEFAULT_CONFIG_DIR


class DatabaseSection(BaseModel):
    """Postgres connection (PostgreSQL 15+ with pgvector)."""

    model_config = _SECTION_CONFIG

    #: SQLAlchemy DSN. Lives in config/secrets.local.yaml — never in the repo, never
    #: in an env var by default (decision B4). Empty means "not configured yet".
    url: SecretStr = SecretStr("")
    echo_sql: bool = False
    pool_size: int = 5
    connect_timeout_s: int = 10
    statement_timeout_ms: int = 30_000
    #: Migration 0001 creates the pgvector extension; set false on a host without it.
    pgvector_required: bool = True

    @property
    def dsn(self) -> str:
        """The plain DSN — call this only where a driver needs the real value."""
        return self.url.get_secret_value()

    @field_validator("url")
    @classmethod
    def _check_scheme(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and not raw.startswith(("postgresql", "postgres")):
            raise ValueError(
                "db.url must be a PostgreSQL SQLAlchemy DSN, e.g. "
                "postgresql+psycopg://user:password@localhost:5432/trend_analyst"
            )
        return value

    @property
    def configured(self) -> bool:
        return bool(self.url.get_secret_value())


class LLMSection(BaseModel):
    """Gate provider credentials and the local fallback (spec §6.3)."""

    model_config = _SECTION_CONFIG

    gemini_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    cerebras_api_key: SecretStr = SecretStr("")
    ollama_base_url: str = "http://localhost:11434"

    @field_validator("ollama_base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("llm.ollama_base_url must start with http:// or https://")
        return value


class TierASection(BaseModel):
    """Tier-A enrichment credentials (spec Appendix A). A blank key means the matching
    source stays ``enabled: false`` in sources.yaml — the build never blocks on an
    approval."""

    model_config = _SECTION_CONFIG

    ebay_client_id: SecretStr = SecretStr("")
    ebay_client_secret: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    bestbuy_api_key: SecretStr = SecretStr("")
    serper_api_key: SecretStr = SecretStr("")
    producthunt_token: SecretStr = SecretStr("")
    producthunt_client_id: SecretStr = SecretStr("")
    producthunt_client_secret: SecretStr = SecretStr("")
    walmart_client_id: SecretStr = SecretStr("")
    walmart_client_secret: SecretStr = SecretStr("")
    searchapi_key: SecretStr = SecretStr("")


class GateSection(BaseModel):
    """One LLM gate's hard cap. Budgets are enforced in code, never in prompts."""

    model_config = _SECTION_CONFIG

    max_calls: int = 0
    max_tokens: int = 0

    @field_validator("max_calls", "max_tokens")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("gate budgets cannot be negative")
        return value


class GatesSection(BaseModel):
    """The only three LLM call sites in v1 (spec §6.2)."""

    model_config = _SECTION_CONFIG

    planner: GateSection = GateSection(max_calls=2, max_tokens=8_000)
    judge: GateSection = GateSection(max_calls=20, max_tokens=120_000)
    writer: GateSection = GateSection(max_calls=30, max_tokens=200_000)


class BudgetsSection(BaseModel):
    """Effort budgets for the deterministic pipeline (brief §4: caps live in config)."""

    model_config = _SECTION_CONFIG

    max_candidates_per_gate: int = 200
    max_enrich_calls_per_night: int = 300
    max_writer_revisions: int = 1
    #: Fraction of L0 candidates dropped in L1 (spec §2: prune 95 %).
    prune_fraction: float = 0.95
    #: Hard ceiling on candidates reaching a gate — the LLM never sees unpruned L0.
    top_k: int = 25

    @field_validator("prune_fraction")
    @classmethod
    def _fraction(cls, value: float) -> float:
        if not 0.0 <= value < 1.0:
            raise ValueError("budgets.prune_fraction must be within [0, 1)")
        return value


class RetentionSection(BaseModel):
    """TTL policy (spec §5.4): the raw lake expires, the ledger and snapshots never do."""

    model_config = _SECTION_CONFIG

    raw_items_ttl_days: int = 90
    llm_cache_ttl_days: int = 30


class HealthSection(BaseModel):
    """Alert thresholds for the monitor agent (spec §9, AGENT.md)."""

    model_config = _SECTION_CONFIG

    quota_burn_alert_pct: float = 80.0
    #: Acceptable band for the Judge keep-rate; drift outside it is an alert.
    judge_keep_rate_band: tuple[float, float] = (0.05, 0.40)
    eval_baseline_drop_alert: float = 2.0
    outbox_path: Path = Path("outbox/alerts.jsonl")

    @field_validator("judge_keep_rate_band")
    @classmethod
    def _ordered_band(cls, value: tuple[float, float]) -> tuple[float, float]:
        low, high = value
        if not 0.0 <= low <= high <= 1.0:
            raise ValueError("health.judge_keep_rate_band must be an ordered pair within [0, 1]")
        return value


class Settings(BaseSettings):
    """The resolved configuration. Immutable by design."""

    model_config = SettingsConfigDict(
        env_prefix="TA_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
        validate_default=True,
        # The env file is resolved per call by load_settings, so a checkout with no
        # config/.env behaves identically to one with it, and a stray repo-root .env
        # can never leak into a test run.
        env_file=None,
    )

    app: AppSection = AppSection()
    db: DatabaseSection = DatabaseSection()
    llm: LLMSection = LLMSection()
    tier_a: TierASection = TierASection()
    gates: GatesSection = GatesSection()
    budgets: BudgetsSection = BudgetsSection()
    retention: RetentionSection = RetentionSection()
    health: HealthSection = HealthSection()

    #: YAML files for the next instantiation, highest precedence first. Set by
    #: :func:`load_settings` immediately before constructing an instance — pydantic-settings
    #: resolves sources through a classmethod, so per-call paths have to be passed here.
    _yaml_files: ClassVar[tuple[Path, ...]] = ()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """CLI > environment > .env > secrets file > env YAML > base YAML > defaults."""
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        sources.extend(
            YamlConfigSettingsSource(settings_cls, yaml_file=path) for path in cls._yaml_files
        )
        return tuple(sources)

    def secret_status(self) -> dict[str, bool]:
        """Map of secret path -> whether it is configured. Never returns a value."""
        status: dict[str, bool] = {}

        def walk(prefix: str, model: BaseModel) -> None:
            for name, value in model:
                path = f"{prefix}{name}"
                if isinstance(value, SecretStr):
                    status[path] = bool(value.get_secret_value())
                elif isinstance(value, BaseModel):
                    walk(f"{path}.", value)

        walk("", self)
        return status

    def redacted(self) -> dict[str, Any]:
        """Settings as plain data with every secret replaced by ``"***"`` or ``""``.

        ``""`` means "not configured", which is what health output and logs show.
        """

        def scrub(value: Any) -> Any:
            if isinstance(value, SecretStr):
                return "***" if value.get_secret_value() else ""
            if isinstance(value, BaseModel):
                return {name: scrub(item) for name, item in value}
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [scrub(item) for item in value]
            if isinstance(value, Path):
                return str(value)
            return value

        return {name: scrub(value) for name, value in self}


class SourceInfo(BaseModel):
    """Provenance for one configuration layer: which file, present or not."""

    model_config = ConfigDict(frozen=True)

    label: str
    path: Path
    exists: bool


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def default_config_dir() -> Path:
    """Config directory: ``TA_CONFIG_DIR`` when set, otherwise the repo's ``config/``."""
    override = os.environ.get("TA_CONFIG_DIR")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_DIR


def describe_sources(config_dir: Path, env_name: str) -> list[SourceInfo]:
    """The YAML layers, highest precedence first, with existence flags."""
    return [
        SourceInfo(label="secrets", path=config_dir / "secrets.local.yaml",
                   exists=(config_dir / "secrets.local.yaml").exists()),
        SourceInfo(label=f"env:{env_name}", path=config_dir / f"settings.{env_name}.yaml",
                   exists=(config_dir / f"settings.{env_name}.yaml").exists()),
        SourceInfo(label="base", path=config_dir / "settings.yaml",
                   exists=(config_dir / "settings.yaml").exists()),
    ]


def parse_overrides(pairs: Sequence[str]) -> dict[str, str]:
    """Turn ``["app.log_level=DEBUG"]`` into ``{"app.log_level": "DEBUG"}``.

    Raises:
        ConfigError: a pair is not of the form ``section.key=value``.
    """
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise ConfigError(f"--set expects section.key=value, got {pair!r}")
        parsed[key.strip()] = value
    return parsed


def load_settings(
    config_dir: Path | str | None = None,
    *,
    env_name: str | None = None,
    overrides: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, str] | None = None,
) -> Settings:
    """Load the layered configuration.

    Args:
        config_dir: directory holding ``settings*.yaml``; defaults to
            :func:`default_config_dir`.
        env_name: environment name selecting ``settings.<env>.yaml``; defaults to
            ``TA_ENV`` or ``"dev"``.
        overrides: dotted keys applied as the highest-priority layer (same meaning as
            ``--set`` on the command line).
        cli_overrides: alias for ``overrides``, kept so call sites can be explicit about
            where the values came from.

    Raises:
        ConfigError: the config directory is missing or a YAML layer cannot be parsed.
    """
    directory = Path(config_dir).expanduser() if config_dir is not None else default_config_dir()
    if not directory.is_dir():
        raise ConfigError(f"config directory does not exist: {directory}")

    environment = env_name or os.environ.get(ENV_NAME_VAR) or DEFAULT_ENV_NAME
    layers = describe_sources(directory, environment)

    # Only existing files become sources: a missing layer is normal (a fresh checkout has
    # no secrets file) and must not be an error.
    Settings._yaml_files = tuple(layer.path for layer in layers if layer.exists)
    env_file = directory / ".env"
    Settings.model_config["env_file"] = env_file if env_file.exists() else None
    Settings.model_config["env_file_encoding"] = "utf-8"

    nested: dict[str, Any] = {"app": {"config_dir": directory}}
    merged = dict(cli_overrides or {})
    merged.update(overrides or {})
    for dotted, raw in merged.items():
        section, _, field = dotted.partition(".")
        if not field:
            raise ConfigError(f"override expects section.key, got {dotted!r}")
        nested.setdefault(section, {})[field] = raw

    try:
        return Settings(**nested)
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError, YAML scanner error, ...
        raise ConfigError(f"invalid configuration in {directory}: {exc}") from exc
