"""Tests for the REST surface, driven through FastAPI's test client."""

import pytest
from fastapi.testclient import TestClient

import main
from conftest import MAC


@pytest.fixture(name="client")
def client_fixture(manager, monkeypatch):
    """A test client over the app, with the manager holding one fake device"""
    monkeypatch.setattr(main, "climate_manager", manager)
    # The app captured the manager at import time; point the routes at ours.
    monkeypatch.setattr(main.app.router, "lifespan_context", _no_lifespan)
    return TestClient(main.app)


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
    manager.view_models[MAC] = main.create_view_model()

    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "Gree Climate API"
    assert body["devices"] == [MAC]


def test_device_view_is_returned(client, manager):
    """A known device is exposed with its cached view"""
    view = main.create_view_model()
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
    manager.view_models[MAC] = main.create_view_model()

    response = client.patch(f"/devices/{MAC}", json={"target_temperature": 24})

    assert response.status_code == 204


def test_update_returns_304_when_nothing_changed(client, manager):
    """A command matching the current state reports not modified"""
    manager.view_models[MAC] = main.create_view_model()

    response = client.patch(f"/devices/{MAC}", json={"target_temperature": 21})

    assert response.status_code == 304


@pytest.mark.parametrize("payload", [{"target_humidity": 42}, {"target_humidity": 90}, {"target_temperature": 5}])
def test_invalid_update_is_rejected(client, manager, payload):
    """Out of range values never reach the device"""
    manager.view_models[MAC] = main.create_view_model()

    assert client.patch(f"/devices/{MAC}", json=payload).status_code == 422
