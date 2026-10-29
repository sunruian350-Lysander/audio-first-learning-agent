"""Server-side configuration; credentials are never serialized to clients."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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
    public_origin: str = ""
    access_user: str = ""
    access_password: str = field(default="", repr=False)
    require_access_auth: bool = False

    def __post_init__(self) -> None:
        if self.require_access_auth and not (self.access_user and self.access_password):
            raise ValueError("Render 部署必须配置 AFL_ACCESS_USER 和 AFL_ACCESS_PASSWORD。")
        if bool(self.access_user) != bool(self.access_password):
            raise ValueError("AFL_ACCESS_USER 和 AFL_ACCESS_PASSWORD 必须同时配置。")
        if self.access_user and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.access_user):
            raise ValueError("AFL_ACCESS_USER 只能包含 1–80 个英文字母、数字、点、下划线或连字符。")
        if self.access_password and (
            not 24 <= len(self.access_password) <= 1024
            or any(not 33 <= ord(char) <= 126 for char in self.access_password)
        ):
            raise ValueError("AFL_ACCESS_PASSWORD 必须为 24–1024 位不含空格的可打印 ASCII 字符，建议由平台随机生成。")
        if self.require_access_auth and not self.public_origin:
            raise ValueError("Render 部署缺少 RENDER_EXTERNAL_URL 或 AFL_PUBLIC_ORIGIN。")
        if not self.public_origin:
            return
        # A single explicit HTTPS origin also supplies the trusted public Host.
        # Do not accept wildcard hosts, credentials or paths as configuration.
        try:
            origin = urlsplit(self.public_origin)
            hostname = origin.hostname or ""
            port = origin.port
            labels = hostname.split(".")
            if (origin.scheme != "https" or not hostname or len(hostname) > 253
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                    or origin.username is not None or origin.password is not None
                    or origin.path not in {"", "/"} or "?" in self.public_origin or "#" in self.public_origin
                    or any(char.isspace() for char in self.public_origin)
                    or "\\" in self.public_origin or (port is not None and not 1 <= port <= 65535)
                    or origin.netloc.endswith(":")):
                raise ValueError
        except ValueError:
            raise ValueError("AFL_PUBLIC_ORIGIN 必须是有效的 HTTPS 站点地址，只含域名和可选端口。") from None
        authority = hostname if port in {None, 443} else f"{hostname}:{port}"
        object.__setattr__(self, "public_origin", f"https://{authority}")

    @classmethod
    def from_env(cls) -> "Settings":
        on_render = os.getenv("RENDER", "").lower() == "true"
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
            public_origin=os.getenv("AFL_PUBLIC_ORIGIN", "") or (
                os.getenv("RENDER_EXTERNAL_URL", "") if on_render else ""
            ),
            access_user=os.getenv("AFL_ACCESS_USER", ""),
            access_password=os.getenv("AFL_ACCESS_PASSWORD", ""),
            require_access_auth=on_render,
        )
