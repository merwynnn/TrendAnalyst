"""Layered configuration tests — the precedence matrix, redaction and failure modes.

Every test builds its own throwaway config directory, so nothing here depends on the
machine's real `config/secrets.local.yaml`, on a database, or on the network.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from config.cli import main as cli_main
from config.settings import ConfigError, Settings, describe_sources, load_settings

# The key used to probe layer precedence: one field, one value per layer.
PROBE = "log_level"
PROBE_ENV_VAR = "TA_APP__LOG_LEVEL"
VALUES = {
    "default": "INFO",
    "base_yaml": "WARNING",
    "env_yaml": "ERROR",
    "secrets": "CRITICAL",
    "env_var": "DEBUG",
    "cli": "INFO",  # deliberately distinct from base_yaml's value to prove the source
}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    return directory


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient TA_* variables may reach a test.

    CI exports TA_DB__URL/TA_TEST_DB_URL job-wide for the database tests, and without
    this the file-layer tests below read CI's database instead of their throwaway
    config dir — five failures that only ever happen on CI, never locally.
    """
    for name in list(os.environ):
        if name.startswith("TA_"):
            monkeypatch.delenv(name, raising=False)


def write_layer(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def write_all_layers(directory: Path) -> None:
    write_layer(directory, "settings.yaml", f"app:\n  {PROBE}: {VALUES['base_yaml']}\n")
    write_layer(directory, "settings.dev.yaml", f"app:\n  {PROBE}: {VALUES['env_yaml']}\n")
    write_layer(directory, "secrets.local.yaml", f"app:\n  {PROBE}: {VALUES['secrets']}\n")


# ---------------------------------------------------------------------------
# The precedence matrix: defaults < base YAML < env YAML < secrets < env < CLI
# ---------------------------------------------------------------------------
def test_full_precedence_matrix(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_all_layers(config_dir)
    monkeypatch.setenv(PROBE_ENV_VAR, VALUES["env_var"])

    def load(**kwargs: object) -> str:
        settings = load_settings(config_dir, env_name="dev", **kwargs)  # type: ignore[arg-type]
        return str(getattr(settings.app, PROBE))

    # 1. CLI wins over everything
    assert load(cli_overrides={f"app.{PROBE}": VALUES["cli"]}) == VALUES["cli"]
    # 2. environment beats every file layer
    assert load() == VALUES["env_var"]
    # 3. the secrets file beats both YAML layers
    monkeypatch.delenv(PROBE_ENV_VAR)
    assert load() == VALUES["secrets"]
    # 4. the environment YAML beats the base YAML
    (config_dir / "secrets.local.yaml").unlink()
    assert load() == VALUES["env_yaml"]
    # 5. the base YAML beats the field default
    (config_dir / "settings.dev.yaml").unlink()
    assert load() == VALUES["base_yaml"]
    # 6. and with no YAML at all, the field default applies
    (config_dir / "settings.yaml").unlink()
    assert load() == VALUES["default"]


def test_field_defaults_load_from_an_empty_directory(config_dir: Path) -> None:
    """A config directory with no YAML is valid: field defaults carry the defaults."""
    settings = load_settings(config_dir, env_name="dev")
    assert settings.app.log_level == "INFO"
    assert settings.app.env == "dev"
    assert settings.budgets.prune_fraction == pytest.approx(0.95)
    assert settings.gates.judge.max_calls == 20
    assert settings.retention.raw_items_ttl_days == 90


def test_missing_layers_are_reported_not_fatal(config_dir: Path) -> None:
    write_layer(config_dir, "settings.yaml", "app:\n  log_level: WARNING\n")
    settings = load_settings(config_dir, env_name="dev")

    assert settings.app.log_level == "WARNING"
    layers = {layer.label: layer for layer in describe_sources(config_dir, "dev")}
    assert layers["base"].exists is True
    assert layers["env:dev"].exists is False
    assert layers["secrets"].exists is False


def test_loader_forces_the_config_directory_it_was_given(config_dir: Path) -> None:
    """A YAML layer cannot redirect the loader to a different directory."""
    write_layer(
        config_dir,
        "settings.yaml",
        "app:\n  config_dir: /somewhere/else\n",
    )
    settings = load_settings(config_dir, env_name="dev")
    assert settings.app.config_dir == config_dir


def test_env_name_is_selectable_by_environment(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_layer(config_dir, "settings.prod.yaml", "app:\n  log_json: true\n  env: prod\n")
    monkeypatch.setenv("TA_ENV", "prod")
    settings = load_settings(config_dir)
    assert settings.app.env == "prod"
    assert settings.app.log_json is True


def test_ta_config_dir_environment_variable_is_honoured(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_layer(config_dir, "settings.yaml", "app:\n  log_level: WARNING\n")
    monkeypatch.setenv("TA_CONFIG_DIR", str(config_dir))
    assert load_settings(env_name="dev").app.log_level == "WARNING"


# ---------------------------------------------------------------------------
# Failure modes: loud, typed, actionable
# ---------------------------------------------------------------------------
def test_missing_config_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config directory does not exist"):
        load_settings(tmp_path / "nope", env_name="dev")


def test_unparseable_yaml_is_an_error(config_dir: Path) -> None:
    write_layer(config_dir, "settings.yaml", "app: [unclosed\n  log_level: INFO\n")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_settings(config_dir, env_name="dev")


def test_unknown_key_is_an_error(config_dir: Path) -> None:
    """A typo must stop the run: silently ignoring it would misconfigure the pipeline."""
    write_layer(config_dir, "settings.yaml", "app:\n  log_levle: DEBUG\n")
    with pytest.raises(ConfigError, match="log_levle"):
        load_settings(config_dir, env_name="dev")


def test_non_postgres_dsn_is_an_error(config_dir: Path) -> None:
    write_layer(config_dir, "secrets.local.yaml", 'db:\n  url: "mysql://u:p@localhost/db"\n')
    with pytest.raises(ConfigError, match="PostgreSQL SQLAlchemy DSN"):
        load_settings(config_dir, env_name="dev")


def test_inverted_keep_rate_band_is_an_error(config_dir: Path) -> None:
    write_layer(config_dir, "settings.yaml", "health:\n  judge_keep_rate_band: [0.6, 0.1]\n")
    with pytest.raises(ConfigError, match="judge_keep_rate_band"):
        load_settings(config_dir, env_name="dev")


def test_negative_gate_budget_is_an_error(config_dir: Path) -> None:
    write_layer(config_dir, "settings.yaml", "gates:\n  judge:\n    max_calls: -1\n")
    with pytest.raises(ConfigError, match="cannot be negative"):
        load_settings(config_dir, env_name="dev")


def test_malformed_override_is_an_error(config_dir: Path) -> None:
    with pytest.raises(ConfigError, match=r"expects section\.key"):
        load_settings(config_dir, env_name="dev", cli_overrides={"log_level": "DEBUG"})


# ---------------------------------------------------------------------------
# Secrets: never in repr, never in dumps, never in logs
# ---------------------------------------------------------------------------
def test_secrets_are_redacted_everywhere(config_dir: Path) -> None:
    secret = "sup3r-s3cret-value"  # a fixture literal, not a credential
    write_layer(
        config_dir,
        "secrets.local.yaml",
        "db:\n"
        f'  url: "postgresql+psycopg://u:{secret}@localhost:5432/db"\n'
        "llm:\n"
        f'  gemini_api_key: "{secret}"\n'
        "tier_a:\n"
        f'  github_token: "{secret}"\n',
    )
    settings = load_settings(config_dir, env_name="dev")

    assert settings.db.configured is True
    assert secret in settings.db.dsn  # the driver needs the real value
    assert secret not in repr(settings)
    assert secret not in json.dumps(settings.redacted())
    assert secret not in settings.model_dump_json()

    redacted = settings.redacted()
    assert redacted["db"]["url"] == "***"  # type: ignore[index]
    assert redacted["llm"]["gemini_api_key"] == "***"  # type: ignore[index]

    status = settings.secret_status()
    assert status["db.url"] is True
    assert status["tier_a.github_token"] is True
    assert status["tier_a.serper_api_key"] is False
    assert all(isinstance(flag, bool) for flag in status.values())


def test_unconfigured_secrets_read_as_empty_not_masked(config_dir: Path) -> None:
    settings = load_settings(config_dir, env_name="dev")
    assert settings.db.configured is False
    assert settings.redacted()["db"]["url"] == ""  # type: ignore[index]
    assert settings.secret_status()["llm.gemini_api_key"] is False


def test_committed_secrets_file_is_gitignored(repo_root: Path) -> None:
    """The untracked secrets file must be ignored — a leak here is unrecoverable."""
    if not (repo_root / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        ["git", "check-ignore", "-q", "config/secrets.local.yaml"],
        cwd=repo_root,
        check=False,
    )
    assert result.returncode == 0, "config/secrets.local.yaml is NOT gitignored"


def _leaf_paths(data: dict[str, object], prefix: str = "") -> set[str]:
    """Every leaf path in a nested mapping, dotted."""
    paths: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            paths |= _leaf_paths(value, f"{path}.")
        else:
            paths.add(path)
    return paths


def _model_leaf_paths(model: BaseModel, prefix: str = "") -> set[str]:
    """Every leaf path in a nested pydantic model, dotted."""
    paths: set[str] = set()
    for name, value in model:
        path = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            paths |= _model_leaf_paths(value, f"{path}.")
        else:
            paths.add(path)
    return paths


def _walk_leaves(data: dict[str, object], prefix: str = "") -> Iterator[tuple[str, object]]:
    """Yield ``(dotted_path, value)`` for every leaf in a nested mapping."""
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _walk_leaves(value, f"{path}.")
        else:
            yield path, value


def test_committed_example_template_validates_against_the_model(
    repo_root: Path, config_dir: Path
) -> None:
    """The template must be loadable by the real settings model: if the model and the
    documentation drift apart, this fails instead of a user discovering it by hand."""
    (config_dir / "secrets.local.yaml").write_text(
        (repo_root / "config" / "secrets.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    settings = load_settings(config_dir, env_name="dev")
    # The template's placeholder DSN IS a value, so the database counts as configured;
    # the API keys are blank and must report as "not configured", not as "missing key".
    assert settings.db.configured is True
    assert settings.secret_status()["llm.gemini_api_key"] is False
    assert settings.secret_status()["tier_a.ebay_client_secret"] is False


def test_committed_template_lists_every_secret_the_model_knows(repo_root: Path) -> None:
    """Two directions, both meaningful:

    * every credential the model knows has a slot in the template (no orphan key), and
    * every path in the template is a real model field (no stale key) — the template is
      validated against the model elsewhere too, but this catches it as a set difference.
    """
    template = yaml.safe_load((repo_root / "config" / "secrets.example.yaml").read_text("utf-8"))
    template_paths = _leaf_paths(template)
    secret_paths = set(Settings().secret_status())
    model_paths = _model_leaf_paths(Settings())

    assert secret_paths <= template_paths, (
        f"undocumented credentials: {sorted(secret_paths - template_paths)}"
    )
    assert template_paths <= model_paths, (
        f"template keys the model rejects: {sorted(template_paths - model_paths)}"
    )


def test_committed_template_holds_no_real_values(repo_root: Path) -> None:
    """The committed template keeps every CREDENTIAL blank or as a CHANGE_ME placeholder.

    Non-secret values (the Ollama base URL) are configuration and may be filled in.
    """
    template = yaml.safe_load((repo_root / "config" / "secrets.example.yaml").read_text("utf-8"))
    values = dict(_walk_leaves(template))

    for path in Settings().secret_status():
        value = values[path]
        assert value == "" or "CHANGE_ME" in str(value), (
            f"the committed template has a real value at {path}: {value!r}"
        )


# ---------------------------------------------------------------------------
# The CLI: precedence proven end-to-end, and the operator-facing output
# ---------------------------------------------------------------------------
def test_cli_show_json_reports_layers_and_redacts(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_layer(config_dir, "settings.yaml", "app:\n  log_level: WARNING\n")
    exit_code = cli_main(["show", "--config-dir", str(config_dir), "--env", "dev", "--json"])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"]["app"]["log_level"] == VALUES["base_yaml"]
    layers = {layer["label"]: layer for layer in payload["layers"]}
    assert layers["base"]["exists"] is True
    assert layers["secrets"]["exists"] is False
    assert payload["credentials_configured"]["db.url"] is False


def test_cli_set_beats_the_environment(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bean's acceptance bar: a real CLI run proving CLI > env > YAML > defaults."""
    write_layer(config_dir, "settings.yaml", f"app:\n  {PROBE}: {VALUES['base_yaml']}\n")
    monkeypatch.setenv(PROBE_ENV_VAR, VALUES["env_var"])

    assert cli_main(["show", "--config-dir", str(config_dir), "--env", "dev", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["settings"]["app"][PROBE] == VALUES["env_var"]

    assert (
        cli_main(
            [
                "show",
                "--config-dir",
                str(config_dir),
                "--env",
                "dev",
                "--set",
                f"app.{PROBE}={VALUES['cli']}",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["settings"]["app"][PROBE] == VALUES["cli"]


def test_cli_show_text_prints_no_secret(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_layer(config_dir, "secrets.local.yaml", 'llm:\n  gemini_api_key: "another-secret"\n')
    assert cli_main(["show", "--config-dir", str(config_dir), "--env", "dev"]) == 0
    out = capsys.readouterr().out
    assert "another-secret" not in out
    assert "credentials configured: 1" in out
    assert "llm.gemini_api_key" in out


def test_cli_reports_bad_configuration_with_exit_code_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli_main(["show", "--config-dir", str(tmp_path / "missing")])
    assert exit_code == 1
    assert "configuration error" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The repository's own configuration must load
# ---------------------------------------------------------------------------
def test_repo_configuration_loads(repo_root: Path) -> None:
    settings = load_settings(repo_root / "config", env_name="dev")
    assert settings.app.config_dir == repo_root / "config"
    assert settings.app.log_level == "DEBUG"  # from settings.dev.yaml
    assert settings.budgets.prune_fraction == pytest.approx(0.95)
    assert settings.app.env == "dev"


def test_repo_configuration_has_a_prod_layer(repo_root: Path) -> None:
    settings = load_settings(repo_root / "config", env_name="prod")
    assert settings.app.env == "prod"
    assert settings.app.log_json is True
    assert settings.app.log_level == "INFO"
