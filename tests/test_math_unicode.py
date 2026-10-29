"""Regression tests for PDF mathematical glyphs and malformed surrogate pairs."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import audio_learning.ingestion as ingestion
import audio_learning.providers as providers
from audio_learning.config import Settings
from audio_learning.ingestion import extract_upload, text_chunks
from audio_learning.main import create_app
from audio_learning.providers import CompatibleProvider, DemoProvider
from audio_learning.schemas import SourceChunk
from audio_learning.unicode_utils import normalize_json_strings, normalize_unicode


def test_normalize_retains_valid_mathematical_symbols_and_repairs_surrogate_pair():
    math = "∫₀¹ 𝑥² d𝑥 = ⅓；∑ √∞ α β ≈ ≤ ≥"
    assert normalize_unicode(math) == math
    assert normalize_unicode("\ud835\udc65") == "𝑥"
    assert normalize_unicode("积分：\ud835 + ∫") == "积分：� + ∫"
    assert normalize_unicode(normalize_unicode("积分：\ud835 + ∫")) == "积分：� + ∫"
    assert normalize_json_strings(json.loads(r'{"formula": "\ud835", "valid": "\ud835\udc65"}')) == {
        "formula": "�", "valid": "𝑥"
    }


def test_pdf_extraction_repairs_malformed_text_but_keeps_integrals(monkeypatch):
    class FakePage:
        def extract_text(self):
            return "定积分 ∫₀¹ 𝑥² d𝑥 = 1/3。\n错误字体编码：\ud835；请核对原文。"

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

        def __init__(self, stream):
            pass

    monkeypatch.setattr(ingestion, "PdfReader", FakeReader)
    result = extract_upload("lecture.pdf", b"%PDF-fake", Settings())
    assert len(result) == 1
    assert "∫₀¹ 𝑥² d𝑥" in result[0].text
    assert "�" in result[0].text
    assert result[0].locator.startswith("PDF 第 1 页")
    result[0].text.encode("utf-8")


def test_pasted_text_is_safe_and_source_positions_preserved():
    chunks = text_chunks("\n# 物理\n∫ f(x) dx 和 𝑥；\ud835。\n")
    assert chunks[0].locator == "原文 · 行 3–3"
    assert chunks[0].text == "∫ f(x) dx 和 𝑥；�。"
    chunks[0].text.encode("utf-8")


def test_provider_request_and_nested_model_json_cannot_leak_surrogates(monkeypatch):
    real_client = httpx.Client

    def handler(request):
        payload = json.loads(request.content)
        source = json.loads(payload["messages"][1]["content"])["source_chunks"][0]["text"]
        assert "∫" in source and "�" in source
        request.content.decode("utf-8")
        # Even if the model returned an escaped isolated surrogate in its JSON,
        # it must be repaired *after* decoding the nested JSON document.
        content = json.dumps({"segments": [{"source_index": 0, "title": "公式", "concepts": ["∫"],
                      "script": ["∫₀¹ 𝑥² d𝑥", "缺字：\ud835"], "exercise": None}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    def replacement(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(providers.httpx, "Client", replacement)
    provider = CompatibleProvider(Settings(provider="compatible", model="deepseek-flash",
                      api_base_url="https://api.deepseek.com", api_key="fake-key", json_mode=True))
    chunk = SourceChunk(title="物理", text="∫_0^1 x² dx\ud835", locator="PDF 第 1 页 · 行 1–1")
    segments = provider.build_segments("公式", [chunk])
    assert segments[0].script == ["∫₀¹ 𝑥² d𝑥", "缺字：�"]
    assert segments[0].source_excerpt == "∫_0^1 x² dx�"
    segments[0].model_dump_json()


def test_pdf_course_upload_stores_and_serves_clean_unicode(tmp_path, monkeypatch):
    class FakePage:
        def extract_text(self):
            return "积分 ∫₀¹ 𝑥² d𝑥；错误字符：\ud835。"

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

        def __init__(self, stream):
            pass

    monkeypatch.setattr(ingestion, "PdfReader", FakeReader)
    with TestClient(create_app(data_dir=tmp_path, provider=DemoProvider())) as client:
        result = client.post("/api/courses/upload", data={"title": "热学"},
                             files={"file": ("lecture.pdf", b"%PDF-fake", "application/pdf")})
        assert result.status_code == 201, result.text
        course = result.json()
        assert "∫₀¹ 𝑥²" in course["segments"][0]["source_excerpt"]
        assert "�" in course["segments"][0]["source_excerpt"]
        assert client.get(f"/api/courses/{course['id']}").status_code == 200
