"""Tests for the REST surface, driven through FastAPI's test client."""

import pytest
from fastapi.testclient import TestClient

from conftest import MAC, device_info
from gree_ws import api, models

OTHER_MAC = "aabbcc334455"


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


def test_unknown_device_returns_404(client, discovery):
    """An unknown MAC that does not answer a scan is a 404, not a crash"""
    discovery([])

    assert client.get("/devices/ffffffffffff").status_code == 404


def test_a_unit_that_came_online_later_is_served(client, manager, discovery):
    """A GET for a unit missing from the roster finds it on the network"""
    discovery([device_info(mac=OTHER_MAC, ip="1.1.1.1")])

    response = client.get(f"/devices/{OTHER_MAC}")

    assert response.status_code == 200
    assert response.json()["mac"] == OTHER_MAC
    assert OTHER_MAC in manager.polling_tasks


def test_a_command_to_a_unit_that_came_online_later_lands(client, discovery):
    """A PATCH for a unit missing from the roster finds it and sends the command"""
    discovery([device_info(mac=OTHER_MAC, ip="1.1.1.1")])

    response = client.patch(f"/devices/{OTHER_MAC}", json={"target_temperature": 24})

    assert response.status_code == 204


def test_a_command_to_an_absent_unit_is_404(client, discovery):
    """A PATCH for a unit nowhere on the network is still a 404"""
    discovery([])

    assert client.patch(f"/devices/{OTHER_MAC}", json={"target_temperature": 24}).status_code == 404


def test_a_websocket_command_to_a_unit_that_came_online_later_lands(client, discovery):
    """The WebSocket update finds a missing unit the same way REST does"""
    discovery([device_info(mac=OTHER_MAC, ip="1.1.1.1")])

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "list"
        socket.send_json({"type": "update", "mac": OTHER_MAC, "message_id": "m3", "data": {"target_temperature": 24}})
        reply = socket.receive_json()

    assert reply["type"] == "applied"
    assert reply["mac"] == OTHER_MAC
    assert reply["message_id"] == "m3"


def test_a_websocket_command_to_an_absent_unit_is_not_found(client, discovery):
    """A unit nowhere on the network is reported as not found over WebSocket"""
    discovery([])

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "list"
        socket.send_json({"type": "update", "mac": OTHER_MAC, "message_id": "m4", "data": {"power": True}})
        reply = socket.receive_json()

    assert reply == {"type": "error", "message_id": "m4", "message": "Device not found"}


@pytest.mark.parametrize("mac", [None, "", "AABBCC334455", "aa:bb:cc:33:44:55", "not-a-mac", 123])
def test_a_malformed_websocket_mac_never_triggers_a_scan(client, monkeypatch, mac):
    """Only a well formed address is worth a discovery broadcast"""

    async def scan(*_args, **_kwargs):
        raise AssertionError("a malformed MAC must not trigger a scan")

    monkeypatch.setattr("greeclimate.discovery.Discovery.scan", scan)

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "list"
        socket.send_json({"type": "update", "mac": mac, "message_id": "m5", "data": {"power": True}})
        reply = socket.receive_json()

    assert reply["type"] == "error"
    assert reply["message"] == "Invalid or missing MAC address"


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


def test_a_client_joining_during_an_outage_sees_it_in_the_list(client, manager):
    """Availability is part of the device, so the initial list already carries it"""
    offline = models.create_view_model()
    offline.mac = MAC
    offline.available = False
    manager.view_models[MAC] = offline

    with client.websocket_connect("/ws") as socket:
        listing = socket.receive_json()

    assert listing["type"] == "list"
    assert listing["data"][0]["available"] is False


def test_the_rest_view_carries_availability(client, manager):
    """The same field is served over REST"""
    view = models.create_view_model()
    view.mac = MAC
    view.available = True
    manager.view_models[MAC] = view

    assert client.get(f"/devices/{MAC}").json()["available"] is True
    assert client.get("/devices").json()[0]["available"] is True
