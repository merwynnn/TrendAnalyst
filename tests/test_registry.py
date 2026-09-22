"""Registry tests: the valid case, every hard-fail rule, ordering, and the allowlist.

Each failure-mode test asserts on the *specific* message, so a rule that fires for the
wrong reason is caught too. Nothing here touches the network: the registry validates
module existence with `find_spec`, never by importing a plugin.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from config.cli import main as cli_main
from trend_analyst.sources.registry import (
    PerSourceQuota,
    RegistryError,
    SourceEntry,
    default_registry_path,
    load_registry,
)

BASE_FIELDS: dict[str, str] = {
    "role": "Test source",
    "module": "trend_analyst.sources.tier_s.hn",
    "tier": "S",
    "layers": "[L0]",
    "schedule": "nightly",
    "budget_per_day": "100",
    "rps": "1",
    "enabled": "true",
    "domains": "[example.com]",
}


def entry(**overrides: str) -> str:
    """An indented entry body, with any field overridden ('' removes the field)."""
    fields = dict(BASE_FIELDS)
    for key, value in overrides.items():
        if value == "":
            fields.pop(key, None)
        else:
            fields[key] = value
    return "\n".join(f"    {key}: {value}" for key, value in fields.items())


def doc(entries: dict[str, str], *, version: str = "1", body: str = "") -> str:
    """A complete registry document."""
    lines = [f"version: {version}", "sources:"]
    if body:
        lines.append(body)
    for source_id, block in entries.items():
        lines.append(f"  {source_id}:")
        lines.append(block)
    return "\n".join(lines) + "\n"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_valid_registry_loads(tmp_path: Path) -> None:
    registry = load_registry(write(tmp_path, doc({"test_source": entry()})))

    assert registry.version == 1
    assert len(registry.sources) == 1
    source = registry.sources[0]
    assert source.id == "test_source"
    assert source.tier == "S"
    assert source.layers == ("L0",)
    assert source.budget_per_day == 100
    assert source.rps == 1.0
    assert source.enabled is True
    assert source.domains == ("example.com",)
    assert source.cache_ttl_h == 24  # default (spec §4.2 does not state one)


def test_tier_a_in_l2_is_valid(tmp_path: Path) -> None:
    registry = load_registry(
        write(
            tmp_path,
            doc(
                {
                    "paid_source": entry(
                        tier="A",
                        layers="[L2]",
                        schedule="on_demand",
                        module="trend_analyst.sources.tier_a.ebay",
                    )
                }
            ),
        )
    )
    assert registry.by_id("paid_source").tier == "A"


def test_execution_order_is_file_order(tmp_path: Path) -> None:
    """Spec §4.2: the orchestrator iterates in file order, so file order is execution order."""
    ordered = {"zeta": entry(), "alpha": entry(), "middle": entry()}
    registry = load_registry(write(tmp_path, doc(ordered)))

    assert [source.id for source in registry.sources] == ["zeta", "alpha", "middle"]
    assert [source.id for source in registry.enabled()] == ["zeta", "alpha", "middle"]


def test_repository_registry_is_the_appendix_a_catalog(repo_root: Path) -> None:
    registry = load_registry(default_registry_path(repo_root / "config"))

    assert len(registry.sources) == 22, "15 Tier-S + 7 Tier-A"
    assert len(registry.by_tier("S")) == 15
    assert len(registry.by_tier("A")) == 7

    # Tier S runs in L0 and is enabled; Tier A enriches in L2 and ships disabled until
    # its credential exists (brief §6: the build never blocks on an approval).
    # SearchAPI is the exception that proves the rule: key present, plugin implemented
    # and live-verified, L2 driver wired — so it is enabled, and the gate asserts every
    # enabled Tier-A source actually loads (an enabled stub would fail that check).
    assert all(source.tier == "S" for source in registry.for_layer("L0"))
    assert len(registry.for_layer("L0")) == 15
    assert all(source.layers == ("L2",) for source in registry.by_tier("A"))
    assert all(source.schedule == "on_demand" for source in registry.by_tier("A"))
    assert all(source.enabled for source in registry.by_tier("S"))
    assert {source.id for source in registry.by_tier("A") if source.enabled} == {"searchapi"}
    assert all(source.domains for source in registry.sources)

    # Appendix A order is preserved, first and last.
    assert registry.sources[0].id == "hn_firebase"
    assert registry.sources[-1].id == "searchapi"


def test_repository_registry_matches_the_spec_examples(repo_root: Path) -> None:
    """The four entries spec §4.2 spells out keep those numbers verbatim."""
    registry = load_registry(default_registry_path(repo_root / "config"))

    assert registry.by_id("hn_firebase").budget_per_day == 2000
    assert registry.by_id("hn_firebase").rps == 5
    assert registry.by_id("wiki_pageviews").budget_per_day == 2000
    assert registry.by_id("wiki_pageviews").rps == 4
    assert registry.by_id("serper").budget_per_day == 100
    assert registry.by_id("serper").rps == 0.5
    # Conflict C2: Appendix A's 5,000/day for eBay wins over the §4.2 example's 1,000.
    assert registry.by_id("ebay_browse").budget_per_day == 5000


# ---------------------------------------------------------------------------
# The invariant: Tier A never runs in L0 (spec §4.3)
# ---------------------------------------------------------------------------
def test_tier_a_in_l0_is_a_hard_failure(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        doc(
            {
                "ebay_browse": entry(
                    tier="A",
                    layers="[L0]",
                    module="trend_analyst.sources.tier_a.ebay",
                )
            }
        ),
    )
    with pytest.raises(RegistryError) as excinfo:
        load_registry(path)

    message = str(excinfo.value)
    assert "ebay_browse" in message
    assert "Tier A but declares layer L0" in message
    assert "layers: [L2]" in message  # the message must say how to fix it


def test_tier_a_in_l0_fails_even_when_disabled(tmp_path: Path) -> None:
    """A latent misconfiguration is still a misconfiguration: enabling it later must not
    be the moment the pipeline breaks."""
    path = write(
        tmp_path,
        doc(
            {
                "ebay_browse": entry(
                    tier="A",
                    layers="[L0]",
                    enabled="false",
                    module="trend_analyst.sources.tier_a.ebay",
                )
            }
        ),
    )
    with pytest.raises(RegistryError, match="declares layer L0"):
        load_registry(path)


# ---------------------------------------------------------------------------
# One test per failure mode
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"tier": "B"}, "tier"),
        ({"layers": "[L1]"}, "layers.0"),
        ({"layers": "[L0, L3]"}, "layers.1"),
        ({"schedule": "monthly"}, "schedule"),
        ({"layers": "[]"}, "layers cannot be empty"),
        ({"layers": "[L0, L0]"}, "duplicates"),
        ({"budget_per_day": "0"}, "budget_per_day must be > 0"),
        ({"budget_per_day": "-5"}, "budget_per_day must be > 0"),
        ({"rps": "0"}, "rps must be > 0"),
        ({"rps": "-1"}, "rps must be > 0"),
        ({"cache_ttl_h": "-1"}, "cache_ttl_h cannot be negative"),
        ({"domains": "[]"}, "domains cannot be empty"),
        ({"domains": "[https://example.com/api]"}, "must be a bare host"),
        ({"module": "os"}, "must live under trend_analyst.sources."),
        ({"module": "trend_analyst.sources.tier_s.nonexistent"}, "does not exist"),
        ({"role": ""}, "role"),
        ({"module": ""}, "module"),
        ({"id": ""}, "id"),
    ],
)
def test_invalid_field_values_are_rejected(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    source_id = overrides.pop("id", "test_source") or "TestSource"
    path = write(tmp_path, doc({source_id: entry(**overrides)}))
    with pytest.raises(RegistryError, match=expected):
        load_registry(path)


def test_source_id_must_be_snake_case(tmp_path: Path) -> None:
    """The id is a database key, so its shape is part of the contract."""
    path = write(tmp_path, doc({"BadId": entry()}))
    with pytest.raises(RegistryError, match="lowercase snake_case"):
        load_registry(path)


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    """A typo like budget_per_dy would leave the source on its default leash."""
    path = write(tmp_path, doc({"test_source": entry(budget_per_dy="10")}))
    with pytest.raises(RegistryError, match=r"unknown keys.*budget_per_dy"):
        load_registry(path)


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, f"{doc({'test_source': entry()})}extra: true\n")
    with pytest.raises(RegistryError, match="unknown top-level keys"):
        load_registry(path)


def test_duplicate_source_id_is_rejected(tmp_path: Path) -> None:
    """PyYAML keeps the last duplicate silently; for a DB key that is data loss."""
    text = doc({"test_source": entry()}) + f"  test_source:\n{entry(budget_per_day='42')}"
    path = write(tmp_path, text)
    with pytest.raises(RegistryError, match="duplicate key 'test_source'"):
        load_registry(path)


def test_empty_registry_is_rejected(tmp_path: Path) -> None:
    """"Nothing scanned" must never look like "all quiet"."""
    path = write(tmp_path, "version: 1\nsources: {}\n")
    with pytest.raises(RegistryError, match="registry is empty"):
        load_registry(path)


def test_missing_registry_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="registry not found"):
        load_registry(tmp_path / "nope.yaml")


def test_unparseable_yaml_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "version: 1\nsources: [unclosed\n")
    with pytest.raises(RegistryError, match="cannot parse"):
        load_registry(path)


def test_non_integer_version_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, doc({"test_source": entry()}, version='"1"'))
    with pytest.raises(RegistryError, match="'version' must be an integer"):
        load_registry(path)


def test_missing_sources_key_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "version: 1\n")
    with pytest.raises(RegistryError, match="'sources' must be a mapping"):
        load_registry(path)


def test_entry_must_be_a_mapping(tmp_path: Path) -> None:
    path = write(tmp_path, "version: 1\nsources:\n  test_source: just a string\n")
    with pytest.raises(RegistryError, match="must be a mapping"):
        load_registry(path)


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    path = write(tmp_path, "- just\n- a list\n")
    with pytest.raises(RegistryError, match="expected a mapping at the top level"):
        load_registry(path)


# ---------------------------------------------------------------------------
# Egress allowlist (spec §10)
# ---------------------------------------------------------------------------
def test_allowed_domains_are_derived_from_the_registry(tmp_path: Path) -> None:
    registry = load_registry(
        write(
            tmp_path,
            doc({"test_source": entry(domains="[api.example.com, cdn.example.net]")}),
        )
    )
    assert registry.allowed_domains() == {
        "test_source": ("api.example.com", "cdn.example.net")
    }


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("api.example.com", True),
        ("API.EXAMPLE.COM", True),
        ("api.example.com.", True),  # trailing dot: same host
        ("evil-api.example.com", False),  # subdomains are NOT implicit
        ("example.com", False),
        ("api.example.com.evil.test", False),
        ("", False),
    ],
)
def test_exact_host_matching(tmp_path: Path, host: str, allowed: bool) -> None:
    registry = load_registry(
        write(tmp_path, doc({"test_source": entry(domains="[api.example.com]")}))
    )
    assert registry.host_allowed("test_source", host) is allowed


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("shop.myshopify.com", True),
        ("a.b.myshopify.com", True),
        ("myshopify.com", False),  # the wildcard authorises subdomains only
        ("evil-myshopify.com", False),
    ],
)
def test_wildcard_host_matching(tmp_path: Path, host: str, allowed: bool) -> None:
    registry = load_registry(
        write(tmp_path, doc({"test_source": entry(domains='["*.myshopify.com"]')}))
    )
    assert registry.host_allowed("test_source", host) is allowed


def test_unknown_source_id_is_an_error(tmp_path: Path) -> None:
    registry = load_registry(write(tmp_path, doc({"test_source": entry()})))
    with pytest.raises(RegistryError, match="unknown source id"):
        registry.host_allowed("not_a_source", "example.com")


# ---------------------------------------------------------------------------
# Queries and the startup-cost guarantee
# ---------------------------------------------------------------------------
def test_queries_filter_and_preserve_order(tmp_path: Path) -> None:
    registry = load_registry(
        write(
            tmp_path,
            doc(
                {
                    "one": entry(),
                    "two": entry(enabled="false"),
                    "three": entry(),
                }
            ),
        )
    )
    assert [source.id for source in registry.enabled()] == ["one", "three"]
    assert [source.id for source in registry.disabled()] == ["two"]
    assert [source.id for source in registry.for_layer("L0")] == ["one", "three"]
    assert registry.for_layer("L2") == ()
    assert [source.id for source in registry.by_tier("S")] == ["one", "two", "three"]


def test_quota_plan_exposes_the_daily_leash(tmp_path: Path) -> None:
    registry = load_registry(write(tmp_path, doc({"test_source": entry(budget_per_day="200")})))
    plan = registry.quota_plan()

    assert plan == (PerSourceQuota(source_id="test_source", budget_per_day=200),)
    assert plan[0].remaining == 200
    assert plan[0].burn_pct == 0.0

    half_spent = PerSourceQuota(source_id="test_source", budget_per_day=200, spent_today=160)
    assert half_spent.remaining == 40
    assert half_spent.burn_pct == pytest.approx(80.0)

    overspent = PerSourceQuota(source_id="test_source", budget_per_day=200, spent_today=500)
    assert overspent.remaining == 0
    assert overspent.burn_pct == pytest.approx(250.0)


def test_validation_does_not_import_plugin_modules(tmp_path: Path) -> None:
    """Startup validation must not execute plugin code: find_spec, never import."""
    module = "trend_analyst.sources.tier_s.hn"
    sys.modules.pop(module, None)

    load_registry(write(tmp_path, doc({"test_source": entry(module=module)})))

    assert module not in sys.modules


def test_entry_model_cannot_be_loosened_after_construction() -> None:
    """Sources are frozen: nothing re-tunes a budget at runtime."""
    source = SourceEntry(id="s", role="r", module="trend_analyst.sources.tier_s.hn",
                         tier="S", layers=("L0",), schedule="nightly",
                         budget_per_day=10, rps=1.0, domains=("example.com",))
    with pytest.raises(Exception, match=r"frozen|Instance is frozen"):
        source.budget_per_day = 999  # type: ignore[misc]


def test_summary_counts(tmp_path: Path) -> None:
    registry = load_registry(
        write(
            tmp_path,
            doc(
                {
                    "free": entry(budget_per_day="100"),
                    "paid": entry(
                        tier="A",
                        layers="[L2]",
                        schedule="on_demand",
                        enabled="false",
                        module="trend_analyst.sources.tier_a.ebay",
                        budget_per_day="500",
                    ),
                }
            ),
        )
    )
    assert registry.summary() == {
        "total": 2,
        "enabled": 1,
        "disabled": 1,
        "tier_s": 1,
        "tier_a": 1,
        "l0": 1,
        "l2": 0,
        "daily_budget": 100,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_registry_prints_in_execution_order(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli_main(["registry", "--registry", str(default_registry_path(repo_root / "config"))])
        == 0
    )
    out = capsys.readouterr().out
    assert out.index("hn_firebase") < out.index("wiki_pageviews") < out.index("ebay_browse")
    assert "22 sources (15 Tier S, 7 Tier A)" in out
    # SearchAPI is the one enabled Tier-A source (key present, plugin live-verified).
    assert "16 enabled, 6 disabled" in out


def test_cli_registry_json(repo_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        cli_main(
            ["registry", "--registry", str(default_registry_path(repo_root / "config")), "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1
    assert payload["summary"]["tier_a"] == 7
    assert payload["sources"][0]["id"] == "hn_firebase"
    assert payload["allowed_domains"]["shopify_public"] == ["*.myshopify.com"]


def test_cli_registry_exits_1_with_an_actionable_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bean's acceptance bar: a real CLI run on a bad registry fails loudly."""
    path = write(
        tmp_path,
        doc({"ebay_browse": entry(tier="A", layers="[L0]",
                                  module="trend_analyst.sources.tier_a.ebay")}),
    )
    assert cli_main(["registry", "--registry", str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "registry error" in captured.err
    assert "Tier A but declares layer L0" in captured.err
    assert "layers: [L2]" in captured.err


def test_cli_registry_reports_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["registry", "--registry", str(tmp_path / "absent.yaml")]) == 1
    assert "registry not found" in capsys.readouterr().err


def test_cli_registry_default_path_follows_the_config_dir(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["registry", "--config-dir", str(repo_root / "config")]) == 0
    assert "hn_firebase" in capsys.readouterr().out


def test_registry_file_is_the_committed_catalog(repo_root: Path) -> None:
    """The registry is data, so it must stay readable YAML (no anchors, no tags)."""
    text = (repo_root / "config" / "sources.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert parsed["version"] == 1
    assert len(parsed["sources"]) == 22
