"""Network-free regressions for DeepSeek and generic Chat Completions."""

import json
import traceback

import httpx
import pytest

import audio_learning.providers as providers
from audio_learning.config import Settings
from audio_learning.providers import CompatibleProvider, ProviderError


def api_response(content="OK", finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def mock_http(monkeypatch, handler):
    original_client = httpx.Client

    def replacement(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        kwargs["trust_env"] = False
        return original_client(*args, **kwargs)

    monkeypatch.setattr(providers.httpx, "Client", replacement)


def provider(base="https://api.deepseek.com", *, json_mode=True, key="fake-test-key", model="deepseek-flash"):
    return CompatibleProvider(Settings(api_base_url=base, model=model, api_key=key, json_mode=json_mode))


@pytest.mark.parametrize("structured,json_mode,budget,json_output", [
    (False, True, 4096, False),
    (True, True, 65536, True),
    (True, False, 65536, False),
])
def test_deepseek_request_contract(monkeypatch, structured, json_mode, budget, json_output):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["messages"] == [
            {"role": "system", "content": "Return JSON when requested."},
            {"role": "user", "content": "Test input"},
        ]
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == budget
        assert payload["stream"] is False
        if json_output:
            assert payload["response_format"] == {"type": "json_object"}
        else:
            assert "response_format" not in payload
        return httpx.Response(200, json=api_response('  {"ok":true}  '))

    mock_http(monkeypatch, handler)
    assert provider(json_mode=json_mode)._complete(
        "Return JSON when requested.", "Test input", structured,
    ) == '{"ok":true}'


@pytest.mark.parametrize("base", [
    "https://model.invalid/v1", "http://127.0.0.1:12345/v1",
    "https://api.deepseek.com.model.invalid/v1",
])
@pytest.mark.parametrize("structured", [False, True])
def test_generic_services_receive_no_deepseek_parameters(monkeypatch, base, structured):
    def handler(request):
        payload = json.loads(request.content)
        assert "thinking" not in payload
        assert "max_tokens" not in payload
        assert payload["stream"] is False
        if structured:
            assert payload["response_format"] == {"type": "json_object"}
        else:
            assert "response_format" not in payload
        return httpx.Response(200, json=api_response())

    mock_http(monkeypatch, handler)
    assert provider(base)._complete("system", "user", structured) == "OK"


@pytest.mark.parametrize("base,endpoint", [
    ("https://api.deepseek.com", "https://api.deepseek.com/chat/completions"),
    ("https://api.deepseek.com/", "https://api.deepseek.com/chat/completions"),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
    ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1/chat/completions"),
    ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com/chat/completions"),
    ("https://api.deepseek.com/v1/chat/completions/", "https://api.deepseek.com/v1/chat/completions"),
])
def test_base_and_complete_endpoint_urls(monkeypatch, base, endpoint):
    def handler(request):
        assert str(request.url) == endpoint
        return httpx.Response(200, json=api_response())

    mock_http(monkeypatch, handler)
    assert provider(base)._complete("system", "user") == "OK"


@pytest.mark.parametrize("base", [
    "https://[", "ftp://model.invalid", "https://", "https://model.invalid/a b",
    "https://name:password@model.invalid", "https://model.invalid?key=private",
    "https://model.invalid#fragment", "https://model.invalid:65536",
    "http://api.deepseek.com",
])
def test_invalid_base_urls_fail_before_sending(monkeypatch, base):
    def handler(request):
        pytest.fail("Invalid configuration must not send a request")

    mock_http(monkeypatch, handler)
    with pytest.raises(ValueError):
        provider(base)


@pytest.mark.parametrize("base", ["https://api.deepseek.com", "https://model.invalid/v1"])
@pytest.mark.parametrize("reason", [
    "length", "aborted", "insufficient_system_resource", "content_filter", "tool_calls",
])
def test_interrupted_results_are_not_returned(monkeypatch, base, reason):
    mock_http(monkeypatch, lambda request: httpx.Response(
        200, json=api_response("Incomplete answer", reason),
    ))
    with pytest.raises(ProviderError):
        provider(base)._complete("system", "user")


@pytest.mark.parametrize("official", [False, True])
@pytest.mark.parametrize("omit_field", [False, True])
def test_missing_finish_reason_keeps_generic_compatibility(monkeypatch, official, omit_field):
    response = api_response(finish_reason=None)
    if omit_field:
        del response["choices"][0]["finish_reason"]
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=response))
    client = provider("https://api.deepseek.com" if official else "https://model.invalid/v1")
    if official:
        with pytest.raises(ProviderError):
            client._complete("system", "user")
    else:
        assert client._complete("system", "user") == "OK"


@pytest.mark.parametrize("response", [
    None, [], {}, {"choices": []}, {"choices": [None]},
    {"choices": [{"finish_reason": "stop", "message": None}]},
    api_response(None), api_response(" \n\t "), api_response(123),
])
def test_malformed_or_empty_responses_are_rejected(monkeypatch, response):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=response))
    with pytest.raises(ProviderError):
        provider()._complete("system", "user")


def test_non_json_response_does_not_disclose_body(monkeypatch):
    secret = "private-provider-debug-detail"
    mock_http(monkeypatch, lambda request: httpx.Response(200, text=secret))
    with pytest.raises(ProviderError) as captured:
        provider()._complete("system", "user")
    assert secret not in str(captured.value)
    assert captured.value.__suppress_context__


@pytest.mark.parametrize("status,hint", [
    (301, "接口"), (400, "参数"), (401, "API Key"), (402, "余额"),
    (404, "路径"), (429, "限流"), (502, "网关"), (503, "繁忙"),
])
def test_http_errors_are_actionable_and_do_not_disclose_body(monkeypatch, status, hint):
    secret = "private-upstream-response-detail"
    mock_http(monkeypatch, lambda request: httpx.Response(status, text=secret))
    with pytest.raises(ProviderError) as captured:
        provider()._complete("system", "user")
    assert str(status) in str(captured.value)
    assert hint in str(captured.value)
    assert secret not in str(captured.value)


@pytest.mark.parametrize("error_type,hint", [
    (httpx.InvalidURL, "地址无效"), (httpx.ProxyError, "代理"),
    (httpx.ConnectTimeout, "连接模型服务超时"), (httpx.ReadTimeout, "模型响应超时"),
    (httpx.ConnectError, "无法连接"), (httpx.RemoteProtocolError, "网络异常"),
])
def test_transport_errors_are_sanitized(monkeypatch, error_type, hint):
    secret = "private-api-key-and-proxy-password"

    def handler(request):
        raise error_type(secret)

    mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError) as captured:
        provider()._complete("system", "user")
    assert hint in str(captured.value)
    assert secret not in str(captured.value)
    assert captured.value.__suppress_context__
    rendered = "".join(traceback.format_exception(
        type(captured.value), captured.value, captured.value.__traceback__,
    ))
    assert secret not in rendered


@pytest.mark.parametrize("key,expected", [("", None), (" fake-test-key ", "Bearer fake-test-key")])
def test_optional_credentials_and_trimmed_identifiers(monkeypatch, key, expected):
    def handler(request):
        assert request.headers.get("Authorization") == expected
        assert json.loads(request.content)["model"] == "local-model"
        return httpx.Response(200, json=api_response())

    mock_http(monkeypatch, handler)
    assert provider(
        "http://127.0.0.1:12345/v1", key=key, model=" local-model ",
    )._complete("system", "user") == "OK"
