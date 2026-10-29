"""Public deployment boundary and shared browser/native API behavior."""

import pytest
from fastapi.testclient import TestClient

from audio_learning.config import Settings
from audio_learning.main import create_app
from audio_learning.providers import DemoProvider


def test_server_keeps_browser_and_native_clients_on_one_durable_state(tmp_path):
    settings = Settings(public_origin="https://learn.example.com")
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings),
                    base_url=settings.public_origin) as browser:
        course = browser.post("/api/demo", headers={"origin": settings.public_origin}).json()
        base = f"/api/courses/{course['id']}/learning"
        before = browser.get(base).json()
        # URLSession and other native clients do not send a browser Origin header.
        claimed = browser.post(base + "/commands", json={"type": "claim", "revision": before["revision"]})
        assert claimed.status_code == 200
        state = claimed.json()["state"]
        started = browser.post(base + "/commands", json={
            "type": "start", "revision": state["revision"], "connection_id": state["connection_id"],
        }).json()
        heard = browser.post(base + "/commands", json={
            "type": "heard", "revision": started["state"]["revision"],
            "connection_id": state["connection_id"], "utterance_id": started["utterance"]["id"],
        })
        assert heard.status_code == 200
        saved = heard.json()["state"]
        assert (saved["segment_index"], saved["sentence_index"]) != (0, 0)

    # A restarted server, accessed from a new browser/device, has the same checkpoint.
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings),
                    base_url=settings.public_origin) as reopened:
        restored = reopened.get(base).json()
        assert (restored["segment_index"], restored["sentence_index"]) == (
            saved["segment_index"], saved["sentence_index"],
        )
        assert restored["connection_id"] is None
        assert reopened.get("/api/courses").json()[0]["id"] == course["id"]


def test_public_host_is_explicit_and_local_health_checks_still_work(tmp_path):
    settings = Settings(public_origin="https://learn.example.com")
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings)) as client:
        assert client.get("/health", headers={"host": "learn.example.com"}).status_code == 200
        assert client.get("/health", headers={"host": "127.0.0.1:8000"}).status_code == 200
        assert client.get("/health", headers={"host": "attacker.example"}).status_code == 400
        assert client.get("/health", headers={"host": "sub.learn.example.com"}).status_code == 400


@pytest.mark.parametrize("headers", [
    {"origin": "https://attacker.example"},
    {"origin": "https://sibling.example.com"},
    {"origin": "http://learn.example.com"},
    {"origin": "null"},
    {"origin": "https://learn.example.com", "sec-fetch-site": "cross-site"},
    {"origin": "https://attacker.example", "x-forwarded-host": "attacker.example",
     "x-forwarded-proto": "https"},
])
def test_public_server_rejects_cross_origin_writes_without_creating_data(tmp_path, headers):
    settings = Settings(public_origin="https://learn.example.com")
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings),
                    base_url=settings.public_origin) as client:
        assert client.post("/api/demo", headers=headers).status_code == 403
        assert client.get("/api/courses").json() == []


def test_proxy_hop_does_not_break_browser_writes(tmp_path):
    settings = Settings(public_origin="https://learn.example.com")
    # The public TLS connection may reach the application over plain internal HTTP.
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings),
                    base_url="http://learn.example.com") as client:
        assert client.post("/api/demo", headers={"origin": settings.public_origin}).status_code == 201


@pytest.mark.parametrize("origin", [
    "http://learn.example.com", "learn.example.com", "https://*", "https://*.example.com",
    "https://user:password@learn.example.com", "https://learn.example.com/path",
    "https://learn.example.com?secret=value", "https://learn.example.com#fragment",
    "https://learn.example.com:0", "https://learn.example.com:65536", "https://learn.example.com:",
    "https://learn.example.com\n", "https://learn.example.com\\other", "https://.example.com",
])
def test_invalid_public_origin_fails_closed(origin):
    with pytest.raises(ValueError, match="AFL_PUBLIC_ORIGIN"):
        Settings(public_origin=origin)


def test_origin_normalization_and_environment(monkeypatch):
    monkeypatch.setenv("AFL_PUBLIC_ORIGIN", "https://LEARN.example.com:443/")
    assert Settings.from_env().public_origin == "https://learn.example.com"
    assert Settings(public_origin="https://learn.example.com:8443").public_origin.endswith(":8443")


def test_public_config_does_not_expose_provider_credentials(tmp_path):
    settings = Settings(public_origin="https://learn.example.com", api_key="private-text-key",
                        realtime_api_key="private-voice-key")
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings),
                    base_url=settings.public_origin) as client:
        response = client.get("/api/config")
        assert response.status_code == 200
        assert "private-text-key" not in response.text
        assert "private-voice-key" not in response.text
