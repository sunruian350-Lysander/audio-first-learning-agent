"""Wire contracts are simulated; these tests do not claim a live voice session."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from audio_learning.config import Settings
from audio_learning.main import create_app
from audio_learning.providers import DemoProvider
from audio_learning.voice import OpenAIRealtimeProvider, VoiceError


def test_unified_webrtc_contract_and_no_automatic_course_responses():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(201, text="v=0\r\ns=voice-test\r\n", headers={"content-type": "application/sdp"})

    settings = Settings(realtime_api_key="test-private-credential")
    provider = OpenAIRealtimeProvider(settings, httpx.MockTransport(handler))
    assert provider.connect("v=0\r\ns=offer-test\r\n").startswith("v=0")
    request = captured[0]
    assert str(request.url) == "https://api.openai.com/v1/realtime/calls"
    assert request.headers["authorization"] == "Bearer test-private-credential"
    assert "multipart/form-data" in request.headers["content-type"]
    assert b'name="sdp"' in request.content
    assert b'name="session"' in request.content
    assert json.dumps(provider.session_config(), ensure_ascii=False).encode() in request.content
    vad = provider.session_config()["audio"]["input"]["turn_detection"]
    assert vad["create_response"] is False and vad["interrupt_response"] is True
    assert "format" not in provider.session_config()["audio"]["input"]


@pytest.mark.parametrize("failure", ["timeout", "auth", "bad-sdp"])
def test_provider_failure_redacts_upstream_secrets(failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test-private-credential", request=request)
        if failure == "auth":
            return httpx.Response(401, text="test-private-credential")
        return httpx.Response(200, text="test-private-credential")

    provider = OpenAIRealtimeProvider(Settings(realtime_api_key="test-private-credential"), httpx.MockTransport(handler))
    with pytest.raises(VoiceError) as exc:
        provider.connect("v=0\r\ns=offer\r\n")
    assert "test-private-credential" not in str(exc.value)


def test_voice_connection_requires_current_lease_and_preserves_unheard_cursor(tmp_path):
    calls = []

    class FakeVoice:
        name = "openai-realtime"
        configured = True

        def connect(self, sdp):
            calls.append(sdp)
            return "v=0\r\ns=answer\r\n"

    with TestClient(create_app(tmp_path, DemoProvider(), FakeVoice())) as client:
        course = client.post("/api/demo").json()
        base = f"/api/courses/{course['id']}"
        initial = client.get(base + "/learning").json()
        claimed = client.post(base + "/learning/commands", json={"type": "claim", "revision": initial["revision"]}).json()["state"]
        body = {"sdp": "v=0\r\ns=offer\r\n", "connection_id": claimed["connection_id"], "revision": claimed["revision"]}
        answer = client.post(base + "/voice/connect", json=body)
        assert answer.status_code == 200 and answer.headers["content-type"].startswith("application/sdp")
        assert answer.headers["cache-control"] == "no-store"
        assert len(calls) == 1
        started = client.post(base + "/learning/commands", json={**{k: claimed[k] for k in ("revision", "connection_id")}, "type": "start"}).json()
        assert started["utterance"]["kind"] == "lecture"
        assert client.post(base + "/voice/connect", json=body).status_code == 409
        assert len(calls) == 1
        assert client.get(base + "/learning").json()["sentence_index"] == 0
        assert client.get(base + "/learning/events").json()[-1]["kind"] == "learning_start"


def test_voice_disabled_is_actionable_and_cross_origin_cannot_spend(tmp_path):
    with TestClient(create_app(tmp_path, DemoProvider(), settings=Settings())) as client:
        config = client.get("/api/config").json()
        assert config["voice"]["provider"] == "disabled"
        assert not config["voice"]["configured"]
        course = client.post("/api/demo").json()
        base = f"/api/courses/{course['id']}"
        claimed = client.post(base + "/learning/commands", json={"type": "claim", "revision": 0}).json()["state"]
        body = {"sdp": "v=0\r\ns=offer\r\n", "connection_id": claimed["connection_id"], "revision": claimed["revision"]}
        assert client.post(base + "/voice/connect", json=body).status_code == 503
        rejected = client.post(base + "/voice/connect", json=body, headers={"origin": "https://example.org"})
        assert rejected.status_code == 403
        assert client.get(base + "/learning").json() == claimed


def test_voice_failure_and_late_negotiation_cannot_change_progress(tmp_path):
    class RevokedVoice:
        name = "openai-realtime"
        configured = True

        def connect(self, sdp):
            state = app.state.orchestrator.get(course["id"])
            app.state.orchestrator.command(course["id"], {"type": "claim", "revision": state["revision"]})
            return "v=0\r\ns=late\r\n"

    app = create_app(tmp_path, DemoProvider(), RevokedVoice())
    with TestClient(app) as client:
        course = client.post("/api/demo").json()
        base = f"/api/courses/{course['id']}"
        claimed = client.post(base + "/learning/commands", json={"type": "claim", "revision": 0}).json()["state"]
        response = client.post(base + "/voice/connect", json={
            "sdp": "v=0\r\ns=offer\r\n", "connection_id": claimed["connection_id"], "revision": claimed["revision"],
        })
        assert response.status_code == 409
        current = client.get(base + "/learning").json()
        assert current["connection_id"] != claimed["connection_id"]
        assert current["sentence_index"] == 0 and current["utterance"] is None


def test_voice_secrets_not_serialized(tmp_path):
    settings = replace(Settings(), voice_provider="openai-realtime", realtime_api_key="test-private-credential")
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings)) as client:
        response = client.get("/api/config")
        assert response.json()["voice"]["configured"]
        assert "test-private-credential" not in response.text
        assert "api_key" not in response.text


def test_local_service_rejects_untrusted_host_headers(tmp_path):
    with TestClient(create_app(tmp_path, DemoProvider())) as client:
        assert client.get("/health", headers={"host": "attacker.example"}).status_code == 400
        assert client.get("/health", headers={"host": "localhost:8010"}).status_code == 200


def test_text_fallback_shares_cursor_and_revokes_older_voice(tmp_path):
    with TestClient(create_app(tmp_path, DemoProvider())) as client:
        course = client.post("/api/demo").json()
        base = f"/api/courses/{course['id']}"
        claimed = client.post(base + "/learning/commands", json={"type": "claim", "revision": 0}).json()["state"]
        started = client.post(base + "/learning/commands", json={
            "type": "start", "revision": claimed["revision"], "connection_id": claimed["connection_id"],
        }).json()
        legacy = client.get(base + "/session").json()
        updated = client.put(base + "/session", json={
            "segment_index": 1, "sentence_index": 1, "status": "paused", "revision": legacy["revision"],
        })
        assert updated.status_code == 200
        learning = client.get(base + "/learning").json()
        assert (learning["segment_index"], learning["sentence_index"]) == (1, 1)
        assert learning["connection_id"] is None and learning["utterance"] is None
        assert client.post(base + "/learning/commands", json={
            "type": "heard", "revision": started["state"]["revision"],
            "connection_id": claimed["connection_id"], "utterance_id": started["utterance"]["id"],
        }).status_code == 409
        assert client.get(base + "/learning").json() == learning
