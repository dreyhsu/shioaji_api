"""HTTP validation tests using only an injected replay controller double."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
import dashboard


@pytest.fixture
def endpoint(monkeypatch):
    runtime = SimpleNamespace(lock=threading.RLock(), playing=False, cursor=0, bars=[1, 2])
    runtime.snapshot = lambda: {"enabled": True, "playing": runtime.playing}
    runtime.advance = Mock(return_value={"cursor": 1})
    runtime.reset = Mock(return_value={"cursor": 0})
    runtime.arm_entry = Mock(return_value={"state": "armed"})
    runtime.cancel_entry = Mock(return_value={"entry": {"state": "cancelled"}})
    monkeypatch.setattr(dashboard.app.state, "mock_runtime", runtime, raising=False)
    client = TestClient(dashboard.app)
    yield runtime, client
    client.close()


@pytest.mark.parametrize("steps", [0, 601, True, 1.5, "10"])
def test_invalid_step_does_not_advance(endpoint, steps):
    runtime, client = endpoint
    assert client.post("/api/mock/control", json={"action": "step", "steps": steps}).status_code == 422
    runtime.advance.assert_not_called()


def test_pause_step_reset_contracts(endpoint):
    runtime, client = endpoint
    assert client.post("/api/mock/control", json={"action": "play"}).json()["playing"]
    client.post("/api/mock/control", json={"action": "step", "steps": 10})
    runtime.advance.assert_called_once_with(10)
    assert not runtime.playing
    assert client.post("/api/mock/control", json={"action": "reset"}).status_code == 422
    runtime.reset.assert_not_called()
    assert client.post("/api/mock/control", json={"action": "reset", "confirmed": True}).status_code == 200
    runtime.reset.assert_called_once()


@pytest.mark.parametrize("changes", [{"confirmed": False}, {"confirmed": "true"}, {"quantity": 0}, {"quantity": True}, {"curve_id": 0}, {"action": "Short"}])
def test_invalid_arm_cannot_submit(endpoint, changes):
    runtime, client = endpoint
    body = {"curve_id": 1, "contract_code": "TMF209912", "quantity": 1, "confirmed": True} | changes
    assert client.post("/api/mock/curve-entry", json=body).status_code == 422
    runtime.arm_entry.assert_not_called()


@pytest.mark.parametrize("action", ["Buy", "Sell"])
def test_arm_delegates_direction_and_quantity(endpoint, action):
    runtime, client = endpoint
    assert client.post("/api/mock/curve-entry", json={
        "curve_id": 1, "contract_code": "TMF209912", "quantity": 3, "confirmed": True, "action": action,
    }).status_code == 200
    runtime.arm_entry.assert_called_once_with(1, "TMF209912", 3, action=action)


def test_arm_errors_are_reported_and_cancel_delegates(endpoint):
    runtime, client = endpoint
    runtime.arm_entry.side_effect = ValueError("invalid curve")
    response = client.post("/api/mock/curve-entry", json={"curve_id": 1, "contract_code": "TMF209912", "confirmed": True})
    assert response.status_code == 422 and response.json()["detail"] == "invalid curve"
    assert client.delete("/api/mock/curve-entry").status_code == 200
    runtime.cancel_entry.assert_called_once()
