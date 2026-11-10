"""Runtime settings, read from environment variables on every call.

Reading lazily means the server starts and lists its tools before a key is set,
which registry introspection (Glama, Smithery, the MCP Registry) relies on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://miragea.ai/v1"
API_KEY_PAGE = "https://miragea.ai/workspace/api"
INSTALL_GUIDE = "https://miragea.ai/skills/miragea-audio/install.md"
DEFAULT_OUTPUT_DIR = "~/YouTubeDubbing"
DEFAULT_WAIT_TIMEOUT = 900.0
DEFAULT_HTTP_TIMEOUT = 60.0
DEFAULT_CONCURRENCY = 4


class ConfigError(Exception):
    """Raised when the environment is not usable. Messages never contain secrets."""


def _num_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0.")
    return value


def validate_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ConfigError(
            "MIRAGEA_BASE_URL must be an http(s) API root without credentials, query or "
            f"fragment, e.g. {DEFAULT_BASE_URL}"
        )
    return url


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    output_dir: Path
    wait_timeout: float
    http_timeout: float
    concurrency: int

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def projects_dir(self) -> Path:
        return self.output_dir / "projects"

    def __repr__(self) -> str:  # never leak the key through logs or tracebacks
        return (
            f"Settings(api_key={'***' if self.api_key else '<unset>'}, base_url={self.base_url!r}, "
            f"output_dir={str(self.output_dir)!r}, wait_timeout={self.wait_timeout}, "
            f"http_timeout={self.http_timeout}, concurrency={self.concurrency})"
        )

    __str__ = __repr__


def load_settings() -> Settings:
    return Settings(
        api_key=os.environ.get("MIRAGEA_API_KEY", "").strip(),
        base_url=validate_base_url(os.environ.get("MIRAGEA_BASE_URL", "") or DEFAULT_BASE_URL),
        output_dir=Path(
            os.environ.get("DUBBING_OUTPUT_DIR", "") or os.environ.get("MIRAGEA_OUTPUT_DIR", "") or DEFAULT_OUTPUT_DIR
        ).expanduser(),
        wait_timeout=_num_env("MIRAGEA_WAIT_TIMEOUT", DEFAULT_WAIT_TIMEOUT),
        http_timeout=_num_env("MIRAGEA_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT),
        concurrency=max(1, min(16, int(_num_env("DUBBING_CONCURRENCY", DEFAULT_CONCURRENCY)))),
    )


MISSING_KEY_MESSAGE = (
    "MIRAGEA_API_KEY is not set. The voices are rendered by the Miragea voice engine: create a key at "
    f"{API_KEY_PAGE}, add it to the \"env\" block of this server in your MCP client config (or export it "
    "in your shell) and restart the client. Never paste the key into chat."
)
