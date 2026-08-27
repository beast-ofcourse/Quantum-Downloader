"""Configuration loading and precedence resolution.

Precedence (highest to lowest): CLI flags > config file > built-in defaults.

The config file is a TOML file (default: ~/.config/ytchannel/config.toml) with a
single [defaults] table (or flat keys) describing the persistent default flags.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "ytchannel", "config.toml"
)

# Keys that may be persisted in the config file and overridden by the CLI.
CONFIG_KEYS = (
    "output_dir",
    "quality",
    "audio_only",
    "delay",
    "write_thumbnail",
    "write_description",
    "write_subs",
    "cookies",
    "limit",
    "after",
    "before",
    "max_retries",
    "proxy",
    "cookies_from_browser",
    "manifest_backend",
    "concurrency",
    "quiet",
    "verbose",
    "log_file",
    "template",
)


@dataclass
class Config:
    output_dir: str = "./downloads"
    quality: str = "best"
    audio_only: bool = False
    delay: float = 2.0
    write_thumbnail: bool = False
    write_description: bool = False
    write_subs: bool = False
    cookies: Optional[str] = None
    limit: Optional[int] = None
    after: Optional[str] = None
    before: Optional[str] = None
    max_retries: int = 3
    proxy: Optional[str] = None
    cookies_from_browser: Optional[str] = None
    manifest_backend: str = "auto"
    concurrency: int = 1
    quiet: bool = False
    verbose: bool = False
    log_file: Optional[str] = None
    template: Optional[str] = None

    @classmethod
    def from_file(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        cfg = cls()
        if not os.path.exists(path):
            return cfg
        try:
            import tomllib  # Python 3.11+

            with open(path, "rb") as f:
                data = tomllib.load(f)
        except ModuleNotFoundError:
            # Fallback for <3.11 if a TOML lib happens to be present.
            try:
                import tomli as tomllib  # type: ignore

                with open(path, "rb") as f:
                    data = tomllib.load(f)
            except ModuleNotFoundError:
                return cfg
        except (OSError, Exception):
            return cfg

        section = data.get("defaults", data)
        for key in CONFIG_KEYS:
            if key in section:
                value = section[key]
                # Normalize types for known keys.
                if key in (
                    "audio_only",
                    "write_thumbnail",
                    "write_description",
                    "write_subs",
                    "quiet",
                    "verbose",
                ):
                    value = bool(value)
                elif key == "delay":
                    value = float(value)
                elif key == "limit":
                    value = int(value)
                elif key == "max_retries":
                    value = int(value)
                setattr(cfg, key, value)
        return cfg

    def merge_cli(self, cli: dict[str, Any]) -> None:
        """Override with non-None CLI values only."""
        for key, value in cli.items():
            if value is not None:
                setattr(self, key, value)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in CONFIG_KEYS}
