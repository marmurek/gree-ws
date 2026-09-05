"""Settings, loaded from a YAML file and overridable from the environment."""

import argparse
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yaml"


@dataclass(frozen=True)
class AuthSettings:
    """Shared token authorisation, off unless a token is configured"""

    enabled: bool = False
    token: str = ""


@dataclass(frozen=True)
class Settings:
    """Everything the application can be configured with"""

    port: int = 8123
    dev_mode: bool = False
    discovery_timeout: int = 3
    polling_interval: int = 2
    response_timeout: float = 5.0
    verbose: bool = False
    auth: AuthSettings = field(default_factory=AuthSettings)


def _as_bool(value: Any) -> bool:
    """Read a boolean the way both YAML and an environment variable spell it"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _section(document: dict, name: str) -> dict:
    """One section of the configuration file, empty if it is absent"""
    section = document.get(name)
    return section if isinstance(section, dict) else {}


def _setting(section: dict, key: str, env: str, cast: Callable[[Any], Any], default: Any) -> Any:
    """Resolve one setting: the environment wins over the file, which wins over the default"""
    value = os.environ.get(env, section.get(key, default))
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning("Invalid value %r for %s, falling back to %r", value, env, default)
        return default


def load_settings(path: str = DEFAULT_CONFIG_PATH) -> Settings:
    """Read the configuration file, letting the environment override it.

    A missing file is not an error: every setting has a default, so the
    application starts unconfigured just as it did before the file existed.
    """
    document: Any = {}
    config_path = Path(path)

    if config_path.is_file():
        try:
            document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.error("Could not parse %s, using defaults and the environment: %s", path, e)
    else:
        logger.info("No configuration file at %s, using defaults and the environment", path)

    if not isinstance(document, dict):
        logger.error("%s does not contain a mapping, using defaults and the environment", path)
        document = {}

    server = _section(document, "server")
    discovery = _section(document, "discovery")
    polling = _section(document, "polling")
    logs = _section(document, "logging")
    auth = _section(document, "auth")

    token = str(_setting(auth, "token", "AUTH_TOKEN", str, "") or "")
    enabled = _as_bool(_setting(auth, "enabled", "AUTH_ENABLED", _as_bool, False))

    if enabled and not token:
        logger.error("Authorisation is enabled but no token is configured; refusing to run unprotected")
        raise ValueError("auth.enabled is true but auth.token is empty")

    return Settings(
        port=_setting(server, "port", "PORT", int, 8123),
        dev_mode=_setting(server, "dev_mode", "DEV_MODE", _as_bool, False),
        discovery_timeout=_setting(discovery, "timeout", "DISCOVERY_TIMEOUT", int, 3),
        polling_interval=_setting(polling, "interval", "POLLING_INTERVAL", int, 2),
        response_timeout=_setting(polling, "response_timeout", "RESPONSE_TIMEOUT", float, 5.0),
        verbose=_setting(logs, "verbose", "VERBOSE", _as_bool, False),
        auth=AuthSettings(enabled=enabled, token=token),
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the command line, which only says where the configuration lives.

    Unknown arguments are ignored so the module can also be imported by a server
    started with its own command line, such as `uvicorn main:app`.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.description = "Gree Climate API - REST and WebSocket API for controlling Gree air conditioners"
    parser.add_argument("--config", help="Path to the YAML configuration file", default=DEFAULT_CONFIG_PATH)

    args, _unknown = parser.parse_known_args(argv)
    return args


def configure_logging(settings: Settings) -> None:
    """Set the log levels for the application and for greeclimate"""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().setLevel(logging.DEBUG if settings.verbose else logging.INFO)
    logging.getLogger("greeclimate").setLevel(logging.DEBUG if settings.verbose else logging.WARNING)
