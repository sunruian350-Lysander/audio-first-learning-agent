"""Replaceable voice transport. Durable learning state belongs to orchestration.

OpenAI Realtime GA unified WebRTC interface, checked against official docs on
2026-10-27. No provider credential or ephemeral credential is sent to the browser.
"""

import json
from typing import Protocol

import httpx

from .config import Settings


class VoiceError(RuntimeError):
    pass


class VoiceProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    def connect(self, sdp: str) -> str: ...


class OpenAIRealtimeProvider:
    name = "openai-realtime"

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.settings.realtime_api_key and self.settings.realtime_model)

    def session_config(self) -> dict:
        return {
            "type": "realtime",
            "model": self.settings.realtime_model,
            "output_modalities": ["audio"],
            "instructions": (
                "你是中文私人导师。应用提供课程句子或讨论任务。遵循每次任务，"
                "不自行推进课程，不虚构来源或学习者掌握程度。"
            ),
            "audio": {
                "input": {
                    "transcription": {"model": self.settings.transcription_model},
                    "turn_detection": {
                        "type": "server_vad", "create_response": False,
                        "interrupt_response": True, "silence_duration_ms": 600,
                    },
                },
                "output": {"voice": self.settings.realtime_voice},
            },
        }

    def connect(self, sdp: str) -> str:
        if not self.configured:
            raise VoiceError("实时语音尚未配置。请在服务端设置语音密钥和模型，或使用文字与浏览器朗读。")
        try:
            with httpx.Client(timeout=30, transport=self.transport) as client:
                response = client.post(
                    f"{self.settings.realtime_api_base_url}/realtime/calls",
                    headers={"Authorization": f"Bearer {self.settings.realtime_api_key}"},
                    files={
                        "sdp": (None, sdp),
                        "session": (None, json.dumps(self.session_config(), ensure_ascii=False)),
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise VoiceError("语音服务连接超时，已保存的位置不受影响。请重试或使用文字模式。") from exc
        except httpx.HTTPError as exc:
            # Upstream error bodies/URLs can contain sensitive data. Never echo them.
            raise VoiceError("语音服务连接失败。请检查服务端密钥、模型权限及网络后重试。") from exc
        answer = response.text
        if not answer.startswith("v=0") or len(answer) > 200000:
            raise VoiceError("语音服务返回了无效的连接信息。请检查端点配置。")
        return answer
