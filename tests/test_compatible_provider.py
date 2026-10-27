"""Validate the generic HTTP adapter with deterministic, network-free responses."""

import json

import httpx
import pytest

import audio_learning.providers as providers
from audio_learning.config import Settings
from audio_learning.providers import CompatibleProvider, ProviderError
from audio_learning.schemas import SourceChunk


CHUNKS = [SourceChunk(title="参数更新", text="梯度下降根据损失函数的梯度更新模型参数。", locator="PDF 第 7 页 · 行 3–4")]


def valid_document():
    return {"segments": [{"source_index": 0, "title": "理解参数更新", "concepts": ["梯度下降"], "script": ["先观察损失的变化。", "再沿梯度反方向更新参数。"], "exercise": None}]}


def api_response(content, **choice_fields):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop", **choice_fields}]}


def mock_http(monkeypatch, handler):
    original_client = httpx.Client

    def replacement(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(providers.httpx, "Client", replacement)


@pytest.mark.parametrize("json_mode", [False, True])
def test_request_contract_and_source_provenance(monkeypatch, json_mode):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=api_response(json.dumps(valid_document(), ensure_ascii=False)))

    mock_http(monkeypatch, handler)
    provider = CompatibleProvider(Settings(model="test-model", api_base_url="https://model.invalid/v1", api_key="fake-unit-test-key", json_mode=json_mode))
    segments = provider.build_segments("参数学习", CHUNKS)
    assert str(requests[0].url) == "https://model.invalid/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer fake-unit-test-key"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "test-model"
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    if json_mode:
        assert payload["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in payload
    assert segments[0].source_locator == CHUNKS[0].locator
    assert segments[0].source_excerpt == CHUNKS[0].text
    assert json.loads(payload["messages"][1]["content"])["source_chunks"][0]["locator"] == CHUNKS[0].locator


def test_local_compatible_endpoint_can_omit_api_key(monkeypatch):
    def handler(request):
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=api_response(json.dumps(valid_document())))

    mock_http(monkeypatch, handler)
    provider = CompatibleProvider(Settings(model="local-test-model", api_base_url="http://127.0.0.1:12345/v1", api_key=""))
    assert provider.build_segments("本地模型", CHUNKS)


@pytest.mark.parametrize("document", [
    "not JSON",
    "```json\n{}\n```",
    json.dumps({"segments": []}),
    json.dumps({"segments": [{"source_index": 4, "title": "错序", "script": ["正文"]}]}),
    json.dumps({"segments": [{"source_index": 0, "title": "伪造出处", "script": ["正文"], "source_locator": "PDF 第 999 页"}]}),
    json.dumps({"segments": [{"source_index": 0, "title": "空讲稿", "script": []}]}),
    json.dumps({"segments": [{"source_index": 0, "title": "错误类型", "script": "不是句子列表"}]}),
    json.dumps({"segments": [{"source_index": False, "title": "错误索引类型", "script": ["正文"]}]}),
    json.dumps({"segments": [{"source_index": 0, "title": "过长口述句", "script": ["字" * 221]}]}),
    json.dumps({"segments": [{"source_index": 0, "title": "过多口述句", "script": ["正文"] * 13}]}),
    '{"segments": [], "segments": []}',
    json.dumps({"segments": [], "source_locator": "伪造位置"}),
])
def test_malformed_or_untraceable_courses_are_rejected(monkeypatch, document):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=api_response(document)))
    provider = CompatibleProvider(Settings(model="test-model"))
    with pytest.raises(ProviderError, match="结构校验失败"):
        provider.build_segments("示例", CHUNKS)


def test_teaching_metadata_is_separate_from_verbatim_source(monkeypatch):
    document = valid_document()
    document["segments"][0].update({
        "learning_objectives": ["能用自己的话解释梯度下降为何改变参数。"],
        "prerequisites": ["了解函数变化率。"],
        "check_questions": ["学习率过大时，误差一定会变小吗？"],
        "supplemental_notes": ["补充解释：可以将更新方向类比为下山方向，类比有局限。"],
    })
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=api_response(json.dumps(document))))
    segment = CompatibleProvider(Settings(model="test-model")).build_segments("示例", CHUNKS)[0]
    assert segment.learning_objectives == document["segments"][0]["learning_objectives"]
    assert segment.prerequisites == document["segments"][0]["prerequisites"]
    assert segment.check_questions == document["segments"][0]["check_questions"]
    assert segment.supplemental_notes == document["segments"][0]["supplemental_notes"]
    assert segment.source_excerpt == CHUNKS[0].text
    assert "下山" not in segment.source_excerpt


@pytest.mark.parametrize("metadata", ["一个目标", [""], ["字" * 401], ["目标"] * 9, [123]])
def test_invalid_teaching_metadata_rejects_whole_course(monkeypatch, metadata):
    document = valid_document()
    document["segments"][0]["learning_objectives"] = metadata
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=api_response(json.dumps(document))))
    with pytest.raises(ProviderError, match="结构校验失败"):
        CompatibleProvider(Settings(model="test-model")).build_segments("示例", CHUNKS)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_upstream_error_bodies_are_not_disclosed(monkeypatch, status):
    secret = "upstream-private-debug-value"
    mock_http(monkeypatch, lambda request: httpx.Response(status, text=secret))
    provider = CompatibleProvider(Settings(model="test-model"))
    with pytest.raises(ProviderError) as error:
        provider.build_segments("示例", CHUNKS)
    assert str(status) in str(error.value)
    assert secret not in str(error.value)


def test_timeout_has_a_clear_retryable_error(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("transport detail", request=request)

    mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError, match="超时"):
        CompatibleProvider(Settings(model="test-model")).build_segments("示例", CHUNKS)


@pytest.mark.parametrize("response", [api_response("", finish_reason="stop"), api_response("{}", finish_reason="length"), {"choices": []}, {"not": "compatible"}])
def test_missing_empty_and_truncated_text_is_rejected(monkeypatch, response):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=response))
    with pytest.raises(ProviderError):
        CompatibleProvider(Settings(model="test-model")).build_segments("示例", CHUNKS)
