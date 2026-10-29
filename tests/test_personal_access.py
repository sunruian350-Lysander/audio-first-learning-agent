"""Render's single-service boundary must protect both browser and native APIs."""

import base64

import pytest
from fastapi.testclient import TestClient

from audio_learning.config import Settings
from audio_learning.main import create_app
from audio_learning.providers import DemoProvider


USER = "owner"
PASSWORD = "render-test-password-0001"
ORIGIN = "https://zouma-example.onrender.com"


@pytest.fixture
def protected(tmp_path):
    settings = Settings(public_origin=ORIGIN, access_user=USER, access_password=PASSWORD,
                        require_access_auth=True)
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings), base_url=ORIGIN) as client:
        yield client


@pytest.mark.parametrize("path", [
    "/", "/api/config", "/api/courses", "/docs", "/openapi.json", "/static/app.js",
    "/health/", "/health/api/courses", "/health/../api/courses",
    "/health%2f..%2fapi%2fcourses", "/api/courses?health=/health",
])
def test_entire_site_requires_credentials(protected, path):
    response = protected.get(path, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("authorization", [
    "Basic broken-base64", "Basic !!!!", "Bearer ignored", "Basic ",
    "Basic " + base64.b64encode(b"owner").decode(),
    "Basic " + base64.b64encode(f"wrong:{PASSWORD}".encode()).decode(),
    "Basic " + base64.b64encode(b"owner:incorrect").decode(),
    "Basic " + "A" * 9000,
])
def test_bad_credentials_cannot_create_courses(protected, authorization):
    assert protected.post("/api/demo", headers={"authorization": authorization}).status_code == 401
    assert protected.get("/api/courses", auth=(USER, PASSWORD)).json() == []


def test_public_health_is_minimal_and_only_read_only(protected):
    response = protected.get("/health?path=/api/courses")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert protected.head("/health").status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        assert protected.request(method, "/health").status_code == 401
    assert protected.get("/health", headers={"host": "attacker.example"}).status_code == 400


def test_authenticated_browser_and_native_keep_origin_and_secret_boundaries(protected):
    assert protected.get("/", auth=(USER, PASSWORD)).status_code == 200
    # Auth does not grant another website permission to modify the personal workspace.
    assert protected.post("/api/demo", auth=(USER, PASSWORD), headers={
        "origin": "https://attacker.example", "x-forwarded-host": "attacker.example",
    }).status_code == 403
    course = protected.post("/api/demo", auth=(USER, PASSWORD), headers={"origin": ORIGIN})
    assert course.status_code == 201
    # Native clients send Basic credentials without a browser Origin header.
    assert protected.get(f"/api/courses/{course.json()['id']}", auth=(USER, PASSWORD)).status_code == 200
    assert protected.post("/api/demo", auth=(USER, PASSWORD)).status_code == 201
    response = protected.get("/api/config", auth=(USER, PASSWORD))
    assert PASSWORD not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"


def test_authenticated_checkpoint_survives_server_restart(tmp_path):
    settings = Settings(public_origin=ORIGIN, access_user=USER, access_password=PASSWORD,
                        require_access_auth=True)
    authorization = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings), base_url=ORIGIN,
                    headers={"authorization": authorization}) as client:
        course = client.post("/api/demo").json()
        base = f"/api/courses/{course['id']}/learning"
        before = client.get(base).json()
        claimed = client.post(base + "/commands", json={"type": "claim", "revision": before["revision"]}).json()["state"]
        started = client.post(base + "/commands", json={
            "type": "start", "revision": claimed["revision"], "connection_id": claimed["connection_id"],
        }).json()
        heard = client.post(base + "/commands", json={
            "type": "heard", "revision": started["state"]["revision"],
            "connection_id": claimed["connection_id"], "utterance_id": started["utterance"]["id"],
        }).json()["state"]
        assert (heard["segment_index"], heard["sentence_index"]) != (0, 0)
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings), base_url=ORIGIN) as client:
        assert client.get(base).status_code == 401
        restored = client.get(base, auth=(USER, PASSWORD)).json()
        assert (restored["segment_index"], restored["sentence_index"]) == (heard["segment_index"], heard["sentence_index"])
        assert restored["connection_id"] is None


@pytest.fixture
def render_environment(monkeypatch):
    for key in ("AFL_PUBLIC_ORIGIN", "AFL_ACCESS_USER", "AFL_ACCESS_PASSWORD", "RENDER_EXTERNAL_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", ORIGIN)
    return monkeypatch


@pytest.mark.parametrize("username,password", [("", ""), (USER, ""), ("", PASSWORD), (USER, "short")])
def test_render_missing_or_short_access_credentials_fail_closed(render_environment, username, password):
    render_environment.setenv("AFL_ACCESS_USER", username)
    render_environment.setenv("AFL_ACCESS_PASSWORD", password)
    with pytest.raises(ValueError, match="AFL_ACCESS"):
        Settings.from_env()


def test_render_uses_platform_url_and_allows_explicit_domain_override(render_environment):
    render_environment.setenv("AFL_ACCESS_USER", USER)
    render_environment.setenv("AFL_ACCESS_PASSWORD", PASSWORD)
    settings = Settings.from_env()
    assert settings.public_origin == ORIGIN
    assert settings.require_access_auth is True
    assert PASSWORD not in repr(settings)
    render_environment.setenv("AFL_PUBLIC_ORIGIN", "https://learn.example.com")
    assert Settings.from_env().public_origin == "https://learn.example.com"
    render_environment.delenv("AFL_PUBLIC_ORIGIN")
    render_environment.delenv("RENDER_EXTERNAL_URL")
    with pytest.raises(ValueError, match="RENDER_EXTERNAL_URL"):
        Settings.from_env()


def test_non_render_local_defaults_remain_unauthenticated(render_environment, tmp_path):
    render_environment.delenv("RENDER")
    settings = Settings.from_env()
    assert settings.public_origin == ""
    assert settings.access_password == ""
    with TestClient(create_app(tmp_path, DemoProvider(), settings=settings)) as client:
        assert client.get("/").status_code == 200
        assert client.post("/api/demo").status_code == 201


@pytest.mark.parametrize("username,password", [
    ("user:name", PASSWORD), ("含中文", PASSWORD), (USER, "x" * 23),
    (USER, "x" * 1025), (USER, "x" * 24 + "\n"), (USER, "x" * 24 + " "),
])
def test_invalid_personal_access_configuration_fails_closed(username, password):
    with pytest.raises(ValueError, match="AFL_ACCESS"):
        Settings(access_user=username, access_password=password)
