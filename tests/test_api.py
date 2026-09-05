"""Tests for the REST surface, driven through FastAPI's test client."""

import pytest
from fastapi.testclient import TestClient

from conftest import MAC
from gree_ws import api, models


@pytest.fixture(name="client")
def client_fixture(manager, monkeypatch):
    """A test client over an app whose manager holds one fake device"""
    monkeypatch.setattr(api, "GreeClimateManager", lambda _args: manager)
    app = api.create_app(manager.settings)
    # Startup would try to discover real devices; the manager is already loaded.
    monkeypatch.setattr(app.router, "lifespan_context", _no_lifespan)
    return TestClient(app)


def _no_lifespan(_app):
    """Replace startup discovery with nothing"""

    class _Nothing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    return _Nothing()


def test_root_lists_known_devices(client, manager):
    """The root endpoint reports the API identity and the device list"""
    manager.view_models[MAC] = models.create_view_model()

    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "Gree Climate API"
    assert body["devices"] == [MAC]


def test_device_view_is_returned(client, manager):
    """A known device is exposed with its cached view"""
    view = models.create_view_model()
    view.mac = MAC
    manager.view_models[MAC] = view

    response = client.get(f"/devices/{MAC}")

    assert response.status_code == 200
    assert response.json()["mac"] == MAC


def test_unknown_device_returns_404(client):
    """An unknown MAC is a 404, not a crash"""
    assert client.get("/devices/ffffffffffff").status_code == 404


def test_malformed_mac_is_rejected(client):
    """The MAC pattern is enforced on the path"""
    assert client.get("/devices/not-a-mac").status_code == 422


def test_update_returns_204_when_something_changed(client, manager):
    """A command that changes the device reports no content"""
    manager.view_models[MAC] = models.create_view_model()

    response = client.patch(f"/devices/{MAC}", json={"target_temperature": 24})

    assert response.status_code == 204


def test_update_returns_304_when_nothing_changed(client, manager):
    """A command matching the current state reports not modified"""
    manager.view_models[MAC] = models.create_view_model()

    response = client.patch(f"/devices/{MAC}", json={"target_temperature": 21})

    assert response.status_code == 304


@pytest.mark.parametrize("payload", [{"target_humidity": 42}, {"target_humidity": 90}, {"target_temperature": 5}])
def test_invalid_update_is_rejected(client, manager, payload):
    """Out of range values never reach the device"""
    manager.view_models[MAC] = models.create_view_model()

    assert client.patch(f"/devices/{MAC}", json=payload).status_code == 422


def test_an_unreachable_device_reports_service_unavailable(client, manager, device):
    """A command the unit never acknowledges is a 503, not a 500"""
    manager.view_models[MAC] = models.create_view_model()
    device.alive = False

    response = client.patch(f"/devices/{MAC}", json={"target_temperature": 28})

    assert response.status_code == 503


def test_a_successful_command_is_acknowledged_over_websocket(client, manager):
    """A command that lands gets an 'applied' reply, not silence"""
    manager.view_models[MAC] = models.create_view_model()

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "list"
        socket.send_json({"type": "update", "mac": MAC, "message_id": "m1", "data": {"target_temperature": 24}})
        reply = socket.receive_json()

    assert reply["type"] == "applied"
    assert reply["mac"] == MAC
    assert reply["message_id"] == "m1"


def test_an_unchanged_command_still_reports_not_changed(client, manager):
    """The existing not_changed reply is unaffected"""
    manager.view_models[MAC] = models.create_view_model()

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "list"
        socket.send_json({"type": "update", "mac": MAC, "message_id": "m2", "data": {"target_temperature": 21}})
        reply = socket.receive_json()

    assert reply["type"] == "not_changed"
    assert reply["message_id"] == "m2"


def test_a_client_joining_during_an_outage_is_told(client, manager):
    """Availability is announced on change, so a late client needs catching up"""
    manager.view_models[MAC] = models.create_view_model()
    manager.unavailable[MAC] = "no_response"

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "list"
        notice = socket.receive_json()

    assert notice["type"] == "availability"
    assert notice["mac"] == MAC
    assert notice["data"] == {"available": False, "reason": "no_response"}
