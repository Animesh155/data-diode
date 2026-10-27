"""Configuration loader for OSDD Protocol Gateway.

Reads YAML configuration files, performs environment-variable substitution,
and validates the result against a JSON Schema.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import structlog
import yaml

logger: structlog.stdlib.BoundLogger = structlog.get_logger(component="config")

# Regex matching ``${VAR}`` and ``${VAR:-default}``
_ENV_PATTERN: re.Pattern[str] = re.compile(
    r"\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def load_config(path: str) -> dict[str, Any]:
    """Load and validate a YAML configuration file.

    Environment variable references of the form ``${VAR}`` or
    ``${VAR:-default}`` found anywhere in string values are expanded
    before validation.

    Parameters
    ----------
    path:
        Filesystem path to the YAML configuration file.

    Returns
    -------
    dict[str, Any]
        Fully resolved and (optionally) validated configuration dictionary.

    Raises
    ------
    ConfigError
        If the file cannot be read, parsed, or fails schema validation.
    """
    config_path = Path(path)

    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    logger.info("config.loading", path=str(config_path))

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file: {exc}") from exc

    # Substitute environment variables before YAML parsing so that values
    # like ``host: ${MODBUS_HOST:-127.0.0.1}`` work seamlessly.
    resolved_text = _substitute_env_vars(raw_text)

    try:
        config: dict[str, Any] = yaml.safe_load(resolved_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigError("Top-level YAML value must be a mapping")

    # Attempt schema validation if a schema path is discoverable.
    schema_path = _find_schema(config_path)
    if schema_path is not None:
        validate_config(config, str(schema_path))

    logger.info("config.loaded", keys=list(config.keys()))
    return config


def validate_config(config: dict[str, Any], schema_path: str) -> None:
    """Validate *config* against the JSON Schema at *schema_path*.

    Parameters
    ----------
    config:
        Configuration dictionary to validate.
    schema_path:
        Filesystem path to a JSON Schema file.

    Raises
    ------
    ConfigError
        If the schema file is missing, unparseable, or the config does not
        conform.
    """
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError as exc:
        logger.warning(
            "config.validation_skipped",
            reason="jsonschema package not installed",
        )
        return

    spath = Path(schema_path)
    if not spath.is_file():
        raise ConfigError(f"Schema file not found: {spath}")

    try:
        schema = json.loads(spath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot load schema: {exc}") from exc

    try:
        jsonschema.validate(instance=config, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"Configuration validation failed: {exc.message}") from exc
    except jsonschema.SchemaError as exc:
        raise ConfigError(f"Invalid JSON Schema: {exc.message}") from exc

    logger.info("config.validated", schema=str(spath))


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _substitute_env_vars(text: str) -> str:
    """Replace ``${VAR}`` and ``${VAR:-default}`` placeholders in *text*.

    If a variable is not set and no default is provided, a
    :class:`ConfigError` is raised.
    """

    def _replacer(match: re.Match[str]) -> str:
        var = match.group("var")
        default = match.group("default")
        value = os.environ.get(var)

        if value is not None:
            return value
        if default is not None:
            return default

        raise ConfigError(
            f"Environment variable ${{{var}}} is not set and no default provided"
        )

    return _ENV_PATTERN.sub(_replacer, text)


def _find_schema(config_path: Path) -> Path | None:
    """Attempt to locate the gateway JSON Schema relative to *config_path*.

    The heuristic searches for ``gateway_schema.json`` in a ``schema/``
    subdirectory next to the configuration file, or one level up.
    """
    candidates = [
        config_path.parent / "schema" / "gateway_schema.json",
        config_path.parent.parent / "config" / "schema" / "gateway_schema.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
