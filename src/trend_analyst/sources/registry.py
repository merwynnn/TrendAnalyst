"""Source registry: load, validate and query ``config/sources.yaml`` (spec §4.2-§4.3).

The registry is the modularity core of the system. The orchestrator never names a
source: it iterates this file, in file order, and talks to the :class:`SourcePlugin`
interface only. Adding, removing, disabling or moving an endpoint is a change to
``sources.yaml`` — not to pipeline code.

Everything that can be wrong with a registry is a **hard startup failure**, because the
alternative is a run that quietly scans the wrong things:

===============================  =================================================
Rule                             Why it exists
===============================  =================================================
Tier A in L0                     L0 is broad and cheap; Tier A spends quota and runs
                                 in L2 on survivors only (spec §4.3)
Unknown tier / layer / schedule  A typo would silently exclude a source
Empty ``layers``                 A source that runs in no layer never runs
``budget_per_day <= 0``          A zero budget is a source that can never fetch
``rps <= 0``                     Unthrottled load on a free endpoint
Empty ``domains``                Spec §10: the egress allowlist is derived from here
Duplicate source id              PyYAML keeps the LAST duplicate silently — the id is
                                 the DB key, so a duplicate means real data loss
Unresolvable ``module``          A registry entry pointing at no code
Empty registry                   "Nothing scanned" must never look like "all quiet"
===============================  =================================================

Module placement (``tier_s/`` vs ``tier_a/``) is a convention, not a rule: the
registry's ``tier`` and ``layers`` fields are what the pipeline obeys.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from yaml.nodes import MappingNode

from trend_analyst.pipeline.layers import SourceLayer

__all__ = [
    "SOURCE_FIELDS",
    "Layer",
    "PerSourceQuota",
    "Registry",
    "RegistryError",
    "Schedule",
    "SourceEntry",
    "SourceLayer",
    "Tier",
    "default_registry_path",
    "load_registry",
]

Tier = Literal["S", "A"]
#: Sources are declared in L0 (collect) or L2 (enrich) only — see pipeline.layers.
Layer = SourceLayer
Schedule = Literal["nightly", "weekly", "hourly", "on_demand"]

MODULE_PREFIX: Final = "trend_analyst.sources."

#: Every field an entry may carry. Unknown keys are a hard failure: a typo like
#: ``budget_per_dy`` would otherwise leave the source on its default leash.
SOURCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "role",
        "module",
        "tier",
        "layers",
        "schedule",
        "budget_per_day",
        "rps",
        "cache_ttl_h",
        "enabled",
        "domains",
    }
)

_ENTRY_CONFIG = ConfigDict(extra="forbid", frozen=True)


class RegistryError(RuntimeError):
    """A registry that cannot be trusted to run. Always carries the exact reason."""


class SourceEntry(BaseModel):
    """One source, fully specified. Immutable: nothing may re-tune a source at runtime."""

    model_config = _ENTRY_CONFIG

    id: str
    role: str
    module: str
    tier: Tier
    layers: tuple[Layer, ...]
    schedule: Schedule
    budget_per_day: int
    rps: float
    cache_ttl_h: int = 24
    enabled: bool = True
    domains: tuple[str, ...]

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not value or not value.replace("_", "").isalnum() or value != value.lower():
            raise ValueError(f"source id {value!r} must be lowercase snake_case (it is a DB key)")
        return value

    @field_validator("module")
    @classmethod
    def _module_shape(cls, value: str) -> str:
        if not value.startswith(MODULE_PREFIX):
            raise ValueError(f"module {value!r} must live under {MODULE_PREFIX}")
        return value

    @field_validator("layers")
    @classmethod
    def _layers_present_and_unique(cls, value: tuple[Layer, ...]) -> tuple[Layer, ...]:
        if not value:
            raise ValueError("layers cannot be empty: a source that runs in no layer never runs")
        if len(set(value)) != len(value):
            raise ValueError(f"layers {list(value)} contains duplicates")
        return value

    @field_validator("budget_per_day")
    @classmethod
    def _positive_budget(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("budget_per_day must be > 0")
        return value

    @field_validator("rps")
    @classmethod
    def _positive_rps(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("rps must be > 0")
        return value

    @field_validator("cache_ttl_h")
    @classmethod
    def _non_negative_ttl(cls, value: int) -> int:
        if value < 0:
            raise ValueError("cache_ttl_h cannot be negative")
        return value

    @field_validator("domains")
    @classmethod
    def _domains_declared(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError(
                "domains cannot be empty: the egress allowlist is derived from it (spec §10)"
            )
        for pattern in value:
            if "://" in pattern or "/" in pattern:
                raise ValueError(f"domain {pattern!r} must be a bare host, not a URL")
        return value

    @model_validator(mode="after")
    def _tier_a_never_in_l0(self) -> SourceEntry:
        """The invariant of spec §4.3, enforced on every construction path."""
        if self.tier == "A" and "L0" in self.layers:
            raise ValueError(
                f"{self.id!r} is Tier A but declares layer L0. Tier A spends quota and runs "
                "in L2 on survivors only — move it to layers: [L2], or make it Tier S if it "
                "really is keyless and cheap enough for L0."
            )
        return self

    def allows_host(self, host: str) -> bool:
        """Whether this source may call ``host`` (spec §10).

        Matching is exact, plus the explicit ``*.suffix`` wildcard form, which matches
        subdomains only — ``*.example.com`` does NOT authorise ``example.com``. An
        allowlist that quietly widens is not an allowlist.
        """
        target = host.strip().lower().rstrip(".")
        if not target:
            return False
        for pattern in self.domains:
            candidate = pattern.strip().lower().rstrip(".")
            if candidate.startswith("*."):
                suffix = candidate[2:]
                if target.endswith(f".{suffix}"):
                    return True
            elif target == candidate:
                return True
        return False


class PerSourceQuota(BaseModel):
    """What one source may spend in a day, and what it has spent so far (spec §4.3)."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    budget_per_day: int
    spent_today: int = 0

    @property
    def remaining(self) -> int:
        return max(self.budget_per_day - self.spent_today, 0)

    @property
    def burn_pct(self) -> float:
        return 100.0 * self.spent_today / self.budget_per_day if self.budget_per_day else 0.0


class Registry(BaseModel):
    """A validated registry. Queries preserve FILE ORDER, which is execution order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    sources: tuple[SourceEntry, ...]

    def by_id(self, source_id: str) -> SourceEntry:
        for entry in self.sources:
            if entry.id == source_id:
                return entry
        raise RegistryError(f"unknown source id {source_id!r}")

    def enabled(self) -> tuple[SourceEntry, ...]:
        return tuple(entry for entry in self.sources if entry.enabled)

    def disabled(self) -> tuple[SourceEntry, ...]:
        return tuple(entry for entry in self.sources if not entry.enabled)

    def for_layer(self, layer: Layer) -> tuple[SourceEntry, ...]:
        """Enabled sources that run in ``layer``, in file order. The orchestrator's loop."""
        return tuple(entry for entry in self.enabled() if layer in entry.layers)

    def by_tier(self, tier: Tier) -> tuple[SourceEntry, ...]:
        return tuple(entry for entry in self.sources if entry.tier == tier)

    def allowed_domains(self) -> dict[str, tuple[str, ...]]:
        """The egress allowlist, derived from the registry (spec §10)."""
        return {entry.id: entry.domains for entry in self.sources}

    def host_allowed(self, source_id: str, host: str) -> bool:
        """Whether ``source_id`` may call ``host``. Used by the HTTP client from P1 on."""
        return self.by_id(source_id).allows_host(host)

    def quota_plan(self) -> tuple[PerSourceQuota, ...]:
        """The daily leash per source, before any spend (health reports burn against this)."""
        return tuple(
            PerSourceQuota(source_id=entry.id, budget_per_day=entry.budget_per_day)
            for entry in self.sources
        )

    def summary(self) -> dict[str, Any]:
        """Counts the CLI and health output report."""
        return {
            "total": len(self.sources),
            "enabled": len(self.enabled()),
            "disabled": len(self.disabled()),
            "tier_s": len(self.by_tier("S")),
            "tier_a": len(self.by_tier("A")),
            "l0": len(self.for_layer("L0")),
            "l2": len(self.for_layer("L2")),
            "daily_budget": sum(entry.budget_per_day for entry in self.enabled()),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    PyYAML silently keeps the last value when a mapping repeats a key, which for
    ``sources:`` means a silently dropped source. The id is a DB key, so that is data
    loss, not a convenience.
    """


def _construct_mapping_no_duplicates(
    loader: _StrictLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise RegistryError(
                f"duplicate key {key!r} in the registry "
                f"(line {key_node.start_mark.line + 1})"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor("tag:yaml.org,2002:map", _construct_mapping_no_duplicates)


def default_registry_path(config_dir: Path) -> Path:
    """``<config_dir>/sources.yaml`` — the registry's canonical location."""
    return Path(config_dir) / "sources.yaml"


def _resolve_module(module: str) -> None:
    """Confirm ``module`` exists WITHOUT importing it (no plugin side effects at startup)."""
    try:
        found = importlib.util.find_spec(module)
    except ModuleNotFoundError as exc:  # a parent package is missing
        raise RegistryError(f"module {module!r} cannot be imported: {exc}") from exc
    if found is None:
        raise RegistryError(
            f"module {module!r} does not exist — add the plugin or set enabled: false"
        )


def _first_error_message(exc: ValidationError) -> str:
    """The first validation error as ``field: message``, without pydantic's noise."""
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"])
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


def _read_document(registry_path: Path) -> Mapping[Any, Any]:
    """Read and parse the file. A missing or unparseable registry is a hard failure."""
    if not registry_path.is_file():
        raise RegistryError(f"registry not found: {registry_path}")
    try:
        raw = yaml.load(registry_path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except RegistryError:
        raise
    except yaml.YAMLError as exc:
        raise RegistryError(f"cannot parse {registry_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise RegistryError(f"{registry_path}: expected a mapping at the top level")
    return raw


def _read_sources_block(registry_path: Path, raw: Mapping[Any, Any]) -> Mapping[Any, Any]:
    """Validate the document's shape and return the ``sources`` mapping."""
    unknown_top_level = set(raw) - {"version", "sources"}
    if unknown_top_level:
        raise RegistryError(
            f"{registry_path}: unknown top-level keys {sorted(unknown_top_level)} "
            "(expected only 'version' and 'sources')"
        )
    if not isinstance(raw.get("version"), int):
        raise RegistryError(f"{registry_path}: 'version' must be an integer")

    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, Mapping):
        raise RegistryError(f"{registry_path}: 'sources' must be a mapping of id -> entry")
    if not sources_raw:
        raise RegistryError(
            f"{registry_path}: the registry is empty. An empty registry would make 'nothing "
            "scanned' look identical to 'all quiet' — declare the sources you intend to run."
        )
    return sources_raw


def _build_entry(registry_path: Path, source_id: Any, body: Any) -> SourceEntry:
    """Validate one entry, then confirm its module exists (without importing it)."""
    if not isinstance(source_id, str):
        raise RegistryError(f"{registry_path}: source ids must be strings, got {source_id!r}")
    if not isinstance(body, Mapping):
        raise RegistryError(f"{registry_path}: source {source_id!r} must be a mapping")
    unknown_fields = set(body) - SOURCE_FIELDS
    if unknown_fields:
        raise RegistryError(
            f"{registry_path}: source {source_id!r} has unknown keys {sorted(unknown_fields)}"
        )
    try:
        entry = SourceEntry(id=source_id, **body)
    except ValidationError as exc:
        raise RegistryError(
            f"{registry_path}: invalid source {source_id!r} — {_first_error_message(exc)}"
        ) from exc
    _resolve_module(entry.module)
    return entry


def load_registry(path: Path | str) -> Registry:
    """Load and fully validate a registry file.

    Args:
        path: the ``sources.yaml`` to read.

    Raises:
        RegistryError: the file is missing, unparseable, or violates any rule in the
            module docstring. The message names the offending source and field.
    """
    registry_path = Path(path)
    raw = _read_document(registry_path)
    sources_raw = _read_sources_block(registry_path, raw)
    entries = [
        _build_entry(registry_path, source_id, body)
        for source_id, body in sources_raw.items()
    ]
    return Registry(version=int(raw["version"]), sources=tuple(entries))
