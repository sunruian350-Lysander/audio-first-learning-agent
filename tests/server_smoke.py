"""CI-only end-to-end check of the real Compose stack on https://localhost.

Uses disposable test credentials and a local Caddy certificate, never provider keys.
Run after starting the stack with the server workflow's .env.server fixture.
"""

import base64
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


ORIGIN = "https://localhost"
AUTH = "Basic " + base64.b64encode(b"ci:ci-only-password").decode("ascii")
# Only this local, disposable CI certificate bypasses public-CA validation.
client = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
)


def request(method, path, body=None, *, authenticated=True, headers=None):
    values = {"Authorization": AUTH} if authenticated else {}
    if body is not None:
        values["Content-Type"] = "application/json"
    values.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ORIGIN + path, data=data, headers=values, method=method)
    try:
        response = client.open(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.headers, response.read()


def ready():
    for _ in range(30):
        try:
            status, _, _ = request("GET", "/health")
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise AssertionError("The authenticated local HTTPS gateway did not become ready")


def document(method, path, body=None, *, expected=200, headers=None):
    status, _, content = request(method, path, body, headers=headers)
    assert status == expected, (method, path, status)
    return json.loads(content)


def compose(*args):
    subprocess.run(["docker", "compose", "--env-file", ".env.server", *args], check=True, timeout=180)


def main():
    ready()
    for path in ("/", "/health", "/api/config", "/api/courses", "/openapi.json", "/docs", "/static/app.js"):
        assert request("GET", path, authenticated=False)[0] == 401, path
    assert request("GET", "/health", headers={"Authorization": "Basic Y2k6d3Jvbmc="})[0] == 401
    assert request("GET", "/health")[1].get("Cache-Control") == "no-store"
    for path in ("/", "/docs", "/openapi.json", "/static/app.js"):
        assert request("GET", path)[0] == 200, path
    config = document("GET", "/api/config")
    assert config["mode"] == "demo" and not config["voice"]["configured"]
    assert document("GET", "/api/courses") == []
    assert request("POST", "/api/demo", headers={"Origin": "https://attacker.example"})[0] == 403
    assert document("GET", "/api/courses") == []

    # Native clients omit Origin; browser writes carry the public HTTPS origin.
    course = document("POST", "/api/demo", expected=201)
    base = f"/api/courses/{course['id']}/learning"
    state = document("GET", base)
    browser = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    state = document("POST", base + "/commands", {
        "type": "claim", "revision": state["revision"],
    }, headers=browser)["state"]
    started = document("POST", base + "/commands", {
        "type": "start", "revision": state["revision"], "connection_id": state["connection_id"],
    }, headers=browser)
    saved = document("POST", base + "/commands", {
        "type": "heard", "revision": started["state"]["revision"],
        "connection_id": state["connection_id"], "utterance_id": started["utterance"]["id"],
    }, headers=browser)["state"]
    checkpoint = (saved["segment_index"], saved["sentence_index"])
    assert checkpoint != (0, 0)

    compose("up", "-d", "--force-recreate", "--no-deps", "--wait", "--wait-timeout", "120", "app")
    ready()
    restored = document("GET", base)
    assert (restored["segment_index"], restored["sentence_index"]) == checkpoint
    assert restored["connection_id"] is None and restored["utterance"] is None
    # Exercise the documented backup/restore path, including Docker cp ownership.
    with tempfile.TemporaryDirectory(prefix="zouma-backup-") as temporary:
        backup = Path(temporary) / "data"
        compose("stop", "app")
        compose("cp", "app:/app/data", str(backup))
        assert (backup / "learning.sqlite3").is_file()
        compose("up", "-d", "--wait", "--wait-timeout", "120", "app")
        ready()
        added_later = document("POST", "/api/demo", expected=201)
        compose("stop", "app")
        compose("run", "--rm", "--no-deps", "--user", "0", "--cap-add", "DAC_OVERRIDE",
                "app", "rm", "-f", "/app/data/learning.sqlite3",
                "/app/data/learning.sqlite3-wal", "/app/data/learning.sqlite3-shm")
        compose("cp", str(backup) + "/.", "app:/app/data/")
        compose("run", "--rm", "--no-deps", "--user", "0", "--cap-add", "CHOWN",
                "app", "chown", "-R", "10001:10001", "/app/data")
        compose("up", "-d", "--wait", "--wait-timeout", "120", "app")
        ready()
        restored = document("GET", base)
        assert (restored["segment_index"], restored["sentence_index"]) == checkpoint
        assert all(item["id"] != added_later["id"] for item in document("GET", "/api/courses"))
        # Reading a restored DB is insufficient: the non-root application must write it too.
        document("POST", "/api/demo", expected=201)
    print("Server smoke check passed: HTTPS, authentication, shared API, persistence and writable restore.")


if __name__ == "__main__":
    main()
