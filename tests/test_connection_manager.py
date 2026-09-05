"""Tests for WebSocket fan-out.

These cover the failure that froze the whole application: a lock held across
an await, plus the iteration race it was hiding.
"""

import asyncio
import time

import pytest
from fastapi import WebSocketDisconnect

from gree_ws.manager import ConnectionManager


class FakeWebSocket:
    """A client that records what it received, optionally slowly or not at all"""

    client = None

    def __init__(self, delay: float = 0.0, dead: bool = False):
        self.delay = delay
        self.dead = dead
        self.received: list[str] = []

    async def accept(self):
        """Accept the connection"""

    async def send_text(self, text: str):
        """Record the payload, after an optional delay"""
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.dead:
            raise WebSocketDisconnect()
        self.received.append(text)


@pytest.fixture(name="connections")
def connections_fixture() -> ConnectionManager:
    """An empty connection manager"""
    return ConnectionManager()


@pytest.mark.asyncio
async def test_broadcast_reaches_every_client(connections):
    """Every connected client receives the payload"""
    clients = [FakeWebSocket() for _ in range(3)]
    for client in clients:
        await connections.connect(client)

    await connections.broadcast({"type": "report", "data": {}})

    assert all(len(c.received) == 1 for c in clients)


@pytest.mark.asyncio
async def test_broadcast_does_not_deadlock_when_a_client_connects_midway(connections):
    """A client connecting during a broadcast must not block the event loop.

    Regression test: with a threading.Lock held across the send, this hung
    forever and not even asyncio.wait_for could break it.
    """
    await connections.connect(FakeWebSocket(delay=0.2))

    async def joiner():
        await asyncio.sleep(0.05)
        await connections.connect(FakeWebSocket())

    await asyncio.wait_for(asyncio.gather(connections.broadcast({"type": "report"}), joiner()), timeout=5)


@pytest.mark.asyncio
async def test_broadcast_survives_connect_disconnect_churn(connections):
    """Clients joining and leaving mid-broadcast must not break the iteration.

    Regression test for "Set changed size during iteration".
    """
    stable = [FakeWebSocket(delay=0.01) for _ in range(3)]
    for client in stable:
        await connections.connect(client)

    async def churn():
        for _ in range(30):
            transient = FakeWebSocket()
            await connections.connect(transient)
            await asyncio.sleep(0)
            connections.disconnect(transient)

    async def spam():
        for index in range(30):
            await connections.broadcast({"type": "report", "n": index})
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(churn(), spam()), timeout=30)

    assert all(len(c.received) == 30 for c in stable)


@pytest.mark.asyncio
async def test_broadcast_drops_dead_clients_and_serves_the_rest(connections):
    """A client that went away is removed, without affecting the others"""
    healthy, dead = FakeWebSocket(), FakeWebSocket(dead=True)
    await connections.connect(healthy)
    await connections.connect(dead)

    await connections.broadcast({"type": "report"})

    assert dead not in connections.active_connections
    assert healthy in connections.active_connections
    assert len(healthy.received) == 1


@pytest.mark.asyncio
async def test_broadcast_sends_concurrently(connections):
    """A slow client must not delay the clients behind it"""
    await connections.connect(FakeWebSocket())
    await connections.connect(FakeWebSocket(delay=0.4))

    started = time.monotonic()
    await connections.broadcast({"type": "report"})
    elapsed = time.monotonic() - started

    assert elapsed < 0.8, "sends appear to be serialised"


@pytest.mark.asyncio
async def test_disconnect_is_idempotent(connections):
    """Disconnecting an unknown or already removed client is harmless"""
    client = FakeWebSocket()
    await connections.connect(client)

    connections.disconnect(client)
    connections.disconnect(client)
    connections.disconnect(FakeWebSocket())

    assert not connections.active_connections


@pytest.mark.asyncio
async def test_broadcast_with_no_clients_is_a_no_op(connections):
    """Broadcasting to nobody does not raise"""
    await connections.broadcast({"type": "report"})
