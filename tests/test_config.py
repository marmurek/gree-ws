"""Tests for reading settings from the configuration file and the environment."""

import os

import pytest

from gree_ws.config import DEFAULT_CONFIG_PATH, AuthSettings, Settings, load_settings, parse_args


def write_config(tmp_path, text: str) -> str:
    """Write a configuration file and return its path"""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_the_shipped_configuration_loads():
    """The file in the repository is valid and matches the built-in defaults"""
    settings = load_settings(DEFAULT_CONFIG_PATH)

    assert settings == Settings()


def test_settings_come_from_the_file(tmp_path):
    """Every section is read"""
    path = write_config(
        tmp_path,
        """
        server:
          port: 9000
          dev_mode: true
        discovery:
          timeout: 7
        polling:
          interval: 4
          response_timeout: 2.5
        logging:
          verbose: true
        auth:
          enabled: true
          token: "s3cret"
        """,
    )

    settings = load_settings(path)

    assert settings.port == 9000
    assert settings.dev_mode is True
    assert settings.discovery_timeout == 7
    assert settings.polling_interval == 4
    assert settings.response_timeout == 2.5
    assert settings.verbose is True
    assert settings.auth == AuthSettings(enabled=True, token="s3cret")


def test_a_missing_file_falls_back_to_defaults(tmp_path):
    """The application still starts without a configuration file"""
    settings = load_settings(str(tmp_path / "nothing-here.yaml"))

    assert settings == Settings()


def test_a_malformed_file_falls_back_to_defaults(tmp_path):
    """A broken file is reported, not fatal"""
    path = write_config(tmp_path, "server: [this is not a mapping\n  - neither is this")

    assert load_settings(path) == Settings()


def test_a_file_that_is_not_a_mapping_falls_back_to_defaults(tmp_path):
    """A valid YAML document of the wrong shape is handled too"""
    path = write_config(tmp_path, "- just\n- a\n- list\n")

    assert load_settings(path) == Settings()


def test_the_environment_overrides_the_file(tmp_path, monkeypatch):
    """Environment variables win, which is how the container is configured"""
    path = write_config(tmp_path, "server:\n  port: 9000\npolling:\n  interval: 4\n")
    monkeypatch.setenv("PORT", "7001")
    monkeypatch.setenv("POLLING_INTERVAL", "9")
    monkeypatch.setenv("VERBOSE", "true")

    settings = load_settings(path)

    assert settings.port == 7001
    assert settings.polling_interval == 9
    assert settings.verbose is True


@pytest.mark.parametrize(
    "value, expected", [("true", True), ("1", True), ("yes", True), ("false", False), ("no", False)]
)
def test_booleans_are_read_the_way_people_write_them(tmp_path, monkeypatch, value, expected):
    """Both YAML booleans and the usual environment spellings work"""
    monkeypatch.setenv("VERBOSE", value)

    assert load_settings(str(tmp_path / "absent.yaml")).verbose is expected


def test_an_unreadable_value_falls_back_to_the_default(tmp_path):
    """A setting of the wrong type does not stop the application"""
    path = write_config(tmp_path, "server:\n  port: not-a-number\n")

    assert load_settings(path).port == Settings().port


def test_authorisation_without_a_token_is_refused(tmp_path):
    """Enabling authorisation without a token would silently protect nothing"""
    path = write_config(tmp_path, "auth:\n  enabled: true\n  token: ''\n")

    with pytest.raises(ValueError):
        load_settings(path)


def test_the_command_line_only_locates_the_configuration():
    """Settings are not passed as flags any more"""
    assert parse_args([]).config == DEFAULT_CONFIG_PATH
    assert parse_args(["--config", "other.yaml"]).config == "other.yaml"


def test_another_runners_arguments_are_ignored():
    """Being imported by uvicorn or pytest must not break argument parsing"""
    assert parse_args(["--host", "0.0.0.0", "--reload", "-q"]).config == DEFAULT_CONFIG_PATH


def test_a_blank_environment_variable_does_not_override_the_file(tmp_path, monkeypatch):
    """Compose writes `- PORT=` as an empty string, which must not count as a setting.

    Regression test: a blank override used to discard the file's value and fall
    all the way back to the built-in default.
    """
    path = write_config(tmp_path, "server:\n  port: 8180\n")
    monkeypatch.setenv("PORT", "")

    assert load_settings(path).port == 8180


def test_an_unusable_environment_variable_falls_back_to_the_file(tmp_path, monkeypatch):
    """A typo in an override must not discard the configured value as well"""
    path = write_config(tmp_path, "server:\n  port: 8180\n")
    monkeypatch.setenv("PORT", "not-a-port")

    assert load_settings(path).port == 8180


def test_a_section_that_is_not_a_mapping_is_reported(tmp_path, caplog):
    """`server: 8180` is a plausible mistake and must not fail silently"""
    path = write_config(tmp_path, "server: 8180\n")

    assert load_settings(path).port == Settings().port
    assert "not a mapping" in caplog.text


def test_an_unreadable_file_falls_back_to_defaults(tmp_path):
    """A file mounted into the container may not be readable by the app's user.

    Regression test: only YAML errors were caught, so a permission problem
    crashed the application on startup.
    """
    path = write_config(tmp_path, "server:\n  port: 8180\n")
    os.chmod(path, 0o000)
    try:
        assert load_settings(path) == Settings()
    finally:
        os.chmod(path, 0o644)


@pytest.mark.parametrize(
    "text, field_name",
    [
        ("polling:\n  interval: 0\n", "polling_interval"),
        ("polling:\n  response_timeout: 0\n", "response_timeout"),
        ("discovery:\n  timeout: 0\n", "discovery_timeout"),
        ("server:\n  port: 0\n", "port"),
        ("server:\n  port: 99999\n", "port"),
    ],
)
def test_values_outside_a_usable_range_are_refused(tmp_path, text, field_name):
    """Zero would mean hammering the units, finding nothing, or an unbindable port"""
    path = write_config(tmp_path, text)

    assert getattr(load_settings(path), field_name) == getattr(Settings(), field_name)


def test_an_unrecognised_boolean_is_refused(tmp_path, monkeypatch):
    """A value that is neither true nor false must not quietly mean false"""
    path = write_config(tmp_path, "logging:\n  verbose: true\n")
    monkeypatch.setenv("VERBOSE", "perhaps")

    assert load_settings(path).verbose is True


@pytest.mark.parametrize("word", ["off", "no", "0", "false"])
def test_the_usual_false_spellings_are_understood(tmp_path, monkeypatch, word):
    """An explicit false in the environment turns a file's true off"""
    path = write_config(tmp_path, "logging:\n  verbose: true\n")
    monkeypatch.setenv("VERBOSE", word)

    assert load_settings(path).verbose is False


def test_a_token_is_stripped(tmp_path, monkeypatch):
    """A token read from a secret file usually arrives with a newline"""
    path = write_config(tmp_path, "auth:\n  enabled: true\n")
    monkeypatch.setenv("AUTH_TOKEN", "  s3cret\n")

    assert load_settings(path).auth == AuthSettings(enabled=True, token="s3cret")


def test_a_token_without_authorisation_is_reported(tmp_path, caplog):
    """Setting only a token leaves the API open, which is worth saying out loud"""
    path = write_config(tmp_path, "auth:\n  enabled: false\n  token: 's3cret'\n")

    settings = load_settings(path)

    assert settings.auth.enabled is False
    assert "open to anyone" in caplog.text
