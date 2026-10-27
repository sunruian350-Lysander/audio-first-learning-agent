"""Server-side configuration; credentials are never serialized to clients."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    provider: str = "demo"
    api_base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    json_mode: bool = False
    data_dir: Path = Path("data")
    max_upload_bytes: int = 8 * 1024 * 1024
    max_source_chars: int = 60000
    max_pdf_pages: int = 30
    voice_provider: str = "disabled"
    realtime_api_base_url: str = "https://api.openai.com/v1"
    realtime_api_key: str = ""
    realtime_model: str = "gpt-realtime-2.1"
    realtime_voice: str = "marin"
    transcription_model: str = "gpt-4o-mini-transcribe"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            provider=os.getenv("AFL_PROVIDER", "demo").lower(),
            api_base_url=os.getenv("AFL_API_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("AFL_API_KEY", ""),
            model=os.getenv("AFL_MODEL", ""),
            json_mode=os.getenv("AFL_JSON_MODE", "false").lower() in {"1", "true", "yes"},
            data_dir=Path(os.getenv("AFL_DATA_DIR", "data")),
            voice_provider=os.getenv("AFL_VOICE_PROVIDER", "disabled").lower(),
            realtime_api_base_url=os.getenv("AFL_REALTIME_API_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            realtime_api_key=os.getenv("AFL_REALTIME_API_KEY", "") or os.getenv("OPENAI_API_KEY", ""),
            realtime_model=os.getenv("AFL_REALTIME_MODEL", "gpt-realtime-2.1"),
            realtime_voice=os.getenv("AFL_REALTIME_VOICE", "marin"),
            transcription_model=os.getenv("AFL_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"),
        )
