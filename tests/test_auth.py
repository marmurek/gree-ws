"""Tests for shared token authorisation."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import MAC
from gree_ws import api, models
from gree_ws.auth import PUBLIC_PATHS, WS_POLICY_VIOLATION
from gree_ws.config import AuthSettings

TOKEN = "s3cret-token"


def build_client(manager, monkeypatch, auth: AuthSettings) -> TestClient:
    """A test client over an app configured with the given authorisation"""
    manager.settings = replace(manager.settings, auth=auth)
    manager.view_models[MAC] = models.create_view_model()
    monkeypatch.setattr(api, "GreeClimateManager", lambda _settings: manager)
    app = api.create_app(manager.settings)

    class _Nothing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(app.router, "lifespan_context", lambda _app: _Nothing())
    return TestClient(app)


@pytest.fixture(name="open_client")
def open_client_fixture(manager, monkeypatch) -> TestClient:
    """The default: no authorisation at all"""
    return build_client(manager, monkeypatch, AuthSettings())


@pytest.fixture(name="guarded_client")
def guarded_client_fixture(manager, monkeypatch) -> TestClient:
    """An application that requires the token"""
    return build_client(manager, monkeypatch, AuthSettings(enabled=True, token=TOKEN))


def test_without_authorisation_everything_is_open(open_client):
    """The default behaviour is unchanged"""
    assert open_client.get("/devices").status_code == 200
    with open_client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "list"


def test_a_request_without_a_token_is_rejected(guarded_client):
    """No token means no data"""
    response = guarded_client.get("/devices")

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing or invalid token"


def test_a_request_with_the_wrong_token_is_rejected(guarded_client):
    """A near miss is still a miss"""
    assert guarded_client.get("/devices", headers={"Authorization": f"Bearer {TOKEN}x"}).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": f"Bearer {TOKEN}"}, {"authorization": f"bearer {TOKEN}"}, {"X-API-Key": TOKEN}],
)
def test_a_request_with_the_token_is_served(guarded_client, headers):
    """Both header styles are accepted, case insensitively"""
    assert guarded_client.get("/devices", headers=headers).status_code == 200


def test_commands_are_guarded_too(guarded_client):
    """It is not only reads that need the token"""
    assert guarded_client.patch(f"/devices/{MAC}", json={"target_temperature": 24}).status_code == 401


@pytest.mark.parametrize("path", sorted(PUBLIC_PATHS - {"/docs/oauth2-redirect"}))
def test_public_paths_stay_reachable(guarded_client, path):
    """The health probe and the schema are still available to an orchestrator"""
    assert guarded_client.get(path).status_code == 200


def test_health_reports_ok(open_client):
    """The probe answers regardless of authorisation"""
    assert open_client.get("/health").json() == {"status": "ok"}


def test_a_websocket_without_a_token_is_closed(guarded_client):
    """An unauthorised socket is refused rather than served"""
    with pytest.raises(Exception):  # starlette raises on the immediate close
        with guarded_client.websocket_connect("/ws") as socket:
            socket.receive_json()


def test_a_websocket_with_the_token_in_the_query_is_served(guarded_client):
    """Browsers cannot set headers, so the query parameter has to work"""
    with guarded_client.websocket_connect(f"/ws?token={TOKEN}") as socket:
        assert socket.receive_json()["type"] == "list"


def test_a_websocket_with_the_token_in_a_header_is_served(guarded_client):
    """Clients that can set headers should prefer them"""
    with guarded_client.websocket_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as socket:
        assert socket.receive_json()["type"] == "list"


def test_the_refusal_code_is_a_policy_violation():
    """A rejected socket is closed with the code that says why"""
    assert WS_POLICY_VIOLATION == 1008
