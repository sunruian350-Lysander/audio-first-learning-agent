"""Contract tests for durable learning state, grounding and interruption races."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from audio_learning.main import create_app
from audio_learning.providers import DemoProvider


SOURCE = """# 线性回归\n\n线性回归用一条直线描述输入与输出之间的关系。预测公式是 y = wx + b。\n\n# 损失函数\n\n均方误差衡量预测值与真实值之间的平方差。梯度下降根据损失的梯度更新参数。"""


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, provider=DemoProvider())) as instance:
        yield instance


def new_course(client):
    response = client.post("/api/courses", json={"title": "线性回归入门", "source_text": SOURCE})
    assert response.status_code == 201, response.text
    return response.json()


def session_for(client, course):
    response = client.get(f"/api/courses/{course['id']}/session")
    assert response.status_code == 200, response.text
    return response.json()


def change_cursor(client, course, current, **changes):
    payload = {key: current[key] for key in ("segment_index", "sentence_index", "status", "revision")}
    payload.update(changes)
    return client.put(f"/api/courses/{course['id']}/session", json=payload)


def question(client, course, current, text="均方误差是什么意思？"):
    return client.post(
        f"/api/courses/{course['id']}/questions",
        json={
            "question": text,
            "segment_index": current["segment_index"],
            "sentence_index": current["sentence_index"],
            "revision": current["revision"],
        },
    )


def test_import_is_grounded_and_demo_is_truthful(client):
    course = new_course(client)
    assert course["provider"] == "demo"
    assert course["segments"]
    for segment in course["segments"]:
        assert segment["source_locator"]
        assert segment["source_excerpt"].strip()
        assert segment["source_excerpt"] in SOURCE
        assert segment["script"] and all(isinstance(sentence, str) and sentence for sentence in segment["script"])
    current = session_for(client, course)
    assert (current["segment_index"], current["sentence_index"], current["status"]) == (0, 0, "paused")
    answer = question(client, course, current)
    assert answer.status_code == 200, answer.text
    assert "演示" in answer.json()["qa"][-1]["answer"]
    summaries = client.get("/api/courses").json()
    assert any(row["id"] == course["id"] and row["segment_count"] == len(course["segments"]) for row in summaries)


def test_demo_uses_local_provider_even_when_configured_provider_fails(tmp_path):
    class MustNotCallProvider(DemoProvider):
        name = "unavailable-api"

        def build_segments(self, *args, **kwargs):
            raise AssertionError("Bundled demo must never contact the configured provider")

    with TestClient(create_app(data_dir=tmp_path, provider=MustNotCallProvider())) as instance:
        response = instance.post("/api/demo")
        assert response.status_code == 201, response.text
        course = response.json()
        assert course["provider"] == "demo"
        assert any(segment.get("exercise") for segment in course["segments"])
        answer = question(instance, course, session_for(instance, course))
        assert answer.status_code == 200, answer.text
        assert "演示" in answer.json()["qa"][-1]["answer"]


def test_interruption_checkpoint_survives_answer_and_restart(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, provider=DemoProvider())) as instance:
        course = new_course(instance)
        current = session_for(instance, course)
        # Use a nonzero cursor so a silent reset to the lesson start is caught.
        last_segment = len(course["segments"]) - 1
        last_sentence = len(course["segments"][last_segment]["script"]) - 1
        moved = change_cursor(instance, course, current, segment_index=last_segment, sentence_index=last_sentence, status="playing")
        assert moved.status_code == 200, moved.text
        answer = question(instance, course, moved.json())
        assert answer.status_code == 200, answer.text
        result = answer.json()
        assert result["status"] == "paused"
        assert (result["segment_index"], result["sentence_index"]) == (last_segment, last_sentence)
        assert result["qa"][-1]["source_locator"] == course["segments"][last_segment]["source_locator"]
        assert result["qa"][-1]["sentence_index"] == last_sentence

    with TestClient(create_app(data_dir=tmp_path, provider=DemoProvider())) as restarted:
        saved = session_for(restarted, course)
        assert saved == result
        assert restarted.get(f"/api/courses/{course['id']}").json() == course


def test_stale_revision_never_overwrites_progress(client):
    course = new_course(client)
    initial = session_for(client, course)
    changed = change_cursor(client, course, initial, status="playing")
    assert changed.status_code == 200
    stale = change_cursor(client, course, initial, status="paused")
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["session"] == changed.json()
    stale_question = question(client, course, initial)
    assert stale_question.status_code == 409, stale_question.text
    assert session_for(client, course) == changed.json()


@pytest.mark.parametrize("changes", [{"segment_index": -1}, {"segment_index": 9999}, {"sentence_index": -1}, {"sentence_index": 9999}, {"status": "answering"}])
def test_invalid_progress_is_rejected_without_mutating_session(client, changes):
    course = new_course(client)
    current = session_for(client, course)
    response = change_cursor(client, course, current, **changes)
    assert response.status_code == 422, response.text
    assert session_for(client, course) == current


def test_completion_only_allowed_at_final_sentence(client):
    course = new_course(client)
    current = session_for(client, course)
    invalid = change_cursor(client, course, current, status="completed")
    assert invalid.status_code == 422, invalid.text
    final_segment = len(course["segments"]) - 1
    final_sentence = len(course["segments"][final_segment]["script"]) - 1
    complete = change_cursor(client, course, current, segment_index=final_segment, sentence_index=final_sentence, status="completed")
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "completed"


def test_late_answer_does_not_replace_newer_playback_position(tmp_path):
    started = Event()
    release = Event()

    class DelayedProvider(DemoProvider):
        name = "test-delayed"

        def answer(self, *args, **kwargs):
            started.set()
            assert release.wait(timeout=10), "Test did not release provider"
            return "演示回答：此回答属于先前保存的位置。"

    with TestClient(create_app(data_dir=tmp_path, provider=DelayedProvider())) as instance:
        course = new_course(instance)
        initial = session_for(instance, course)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(question, instance, course, initial)
            try:
                assert started.wait(timeout=10), "Question did not reach provider"
                answering = session_for(instance, course)
                assert answering["status"] == "answering"
                later_segment = len(course["segments"]) - 1
                later_sentence = len(course["segments"][later_segment]["script"]) - 1
                updated = change_cursor(instance, course, answering, segment_index=later_segment, sentence_index=later_sentence, status="playing")
                assert updated.status_code == 200, updated.text
            finally:
                release.set()
            finished = future.result(timeout=10)
        assert finished.status_code == 200, finished.text
        saved = session_for(instance, course)
        assert (saved["segment_index"], saved["sentence_index"], saved["status"]) == (later_segment, later_sentence, "playing")
        assert saved["qa"][-1]["segment_index"] == initial["segment_index"]


def test_failed_answer_preserves_resumable_checkpoint(tmp_path):
    class FailingProvider(DemoProvider):
        name = "test-failing"

        def answer(self, *args, **kwargs):
            raise RuntimeError("Upstream unavailable")

    with TestClient(create_app(data_dir=tmp_path, provider=FailingProvider())) as instance:
        course = new_course(instance)
        initial = session_for(instance, course)
        response = question(instance, course, initial)
        assert response.status_code == 502, response.text
        saved = session_for(instance, course)
        assert (saved["segment_index"], saved["sentence_index"], saved["status"]) == (0, 0, "paused")
        assert saved["revision"] > initial["revision"]
        assert not saved["qa"]


def test_followup_question_receives_prior_course_dialogue(tmp_path):
    histories = []

    class RecordingProvider(DemoProvider):
        name = "test-history"

        def answer(self, course, segment_index, sentence_index, question, history):
            histories.append([entry.question for entry in history])
            return "演示回答：跟随课程上下文。"

    with TestClient(create_app(data_dir=tmp_path, provider=RecordingProvider())) as instance:
        course = new_course(instance)
        first = question(instance, course, session_for(instance, course), "什么是损失？")
        assert first.status_code == 200, first.text
        second = question(instance, course, first.json(), "能再举一个例子吗？")
        assert second.status_code == 200, second.text
        assert histories == [[], ["什么是损失？"]]
        assert len(second.json()["qa"]) == 2


@pytest.mark.parametrize("title,text", [("", SOURCE), ("有效标题", ""), ("有效标题", "   \n\t")])
def test_empty_import_is_rejected(client, title, text):
    response = client.post("/api/courses", json={"title": title, "source_text": text})
    assert response.status_code == 422, response.text
    assert client.get("/api/courses").json() == []


def test_source_and_upload_limits_are_enforced(client):
    limits = client.get("/api/config").json()["limits"]
    response = client.post("/api/courses", json={"title": "超长教材", "source_text": "a" * (limits["max_source_chars"] + 1)})
    assert response.status_code in (413, 422), response.text
    response = client.post("/api/courses/upload", files={"file": ("too-large.txt", b"x" * (limits["max_upload_bytes"] + 1), "text/plain")})
    assert response.status_code in (413, 422), response.text


@pytest.mark.parametrize("filename,content", [("unsupported.exe", b"hello"), ("empty.txt", b""), ("invalid.txt", b"\xff\xfe\xff"), ("broken.pdf", b"not a PDF")])
def test_invalid_uploads_have_actionable_errors(client, filename, content):
    response = client.post("/api/courses/upload", files={"file": (filename, content, "application/octet-stream")})
    assert response.status_code in (400, 415, 422), response.text
    assert response.json().get("detail")
    assert client.get("/api/courses").json() == []


def test_text_upload_preserves_utf8_and_source_name(client):
    response = client.post("/api/courses/upload", files={"file": ("教材.md", SOURCE.encode("utf-8"), "text/markdown")})
    assert response.status_code == 201, response.text
    assert response.json()["source_name"] == "教材.md"
    assert any("均方误差" in segment["source_excerpt"] for segment in response.json()["segments"])


def test_dense_short_lines_still_form_a_valid_demo_course(client):
    text = "\n".join(f"第 {index} 个例子。" for index in range(600))
    response = client.post("/api/courses", json={"title": "密集短行教材", "source_text": text})
    assert response.status_code == 201, response.text
    segments = response.json()["segments"]
    assert segments and all(1 <= len(segment["script"]) <= 60 for segment in segments)
    assert all(segment["source_excerpt"] in text for segment in segments)


def test_scanned_and_encrypted_pdfs_are_rejected(client):
    for encrypted in (False, True):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        if encrypted:
            writer.encrypt("password")
        output = BytesIO()
        writer.write(output)
        response = client.post("/api/courses/upload", files={"file": ("scan.pdf", output.getvalue(), "application/pdf")})
        assert response.status_code in (400, 422), response.text
        assert response.json().get("detail")


def test_text_pdf_produces_real_page_locators(client):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in ("Regression predicts values.", "Squared error measures loss."):
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 260 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    response = client.post("/api/courses/upload", files={"file": ("lesson.pdf", output.getvalue(), "application/pdf")})
    assert response.status_code == 201, response.text
    segments = response.json()["segments"]
    assert any("第 1 页" in segment["source_locator"] and "Regression" in segment["source_excerpt"] for segment in segments)
    assert any("第 2 页" in segment["source_locator"] and "Squared" in segment["source_excerpt"] for segment in segments)


def test_notebook_is_a_download_with_unexecuted_practice_cells(client):
    response = client.post("/api/demo")
    assert response.status_code == 201, response.text
    course = response.json()
    notebook_response = client.get(f"/api/courses/{course['id']}/notebook")
    assert notebook_response.status_code == 200, notebook_response.text
    assert ".ipynb" in notebook_response.headers["content-disposition"]
    notebook = notebook_response.json()
    assert notebook["nbformat"] == 4
    code_cells = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
    assert code_cells
    assert all(cell["execution_count"] is None and cell["outputs"] == [] for cell in code_cells)
    assert any(cell["cell_type"] == "markdown" and "解" in "".join(cell["source"]) for cell in notebook["cells"])


def test_unknown_course_returns_404(client):
    for suffix in ("", "/session", "/notebook"):
        response = client.get(f"/api/courses/not-a-course{suffix}")
        assert response.status_code == 404, response.text


def test_config_has_no_credentials(client):
    response = client.get("/api/config")
    assert response.status_code == 200
    assert "api_key" not in response.text.lower()
    assert "authorization" not in response.text.lower()
    assert response.json()["mode"] == "demo"
    assert client.get("/health").json() == {"status": "ok"}
