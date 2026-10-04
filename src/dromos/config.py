"""Settings loaded from config.toml (repo root) with environment overrides."""
from __future__ import annotations

from pathlib import Path

import tomllib
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DROMOS_", extra="ignore")

    data_path: str = "data"
    posts_path: str = "posts"

    @property
    def data_dir(self) -> Path:
        return (PROJECT_ROOT / self.data_path).resolve()

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def posts_dir(self) -> Path:
        return (PROJECT_ROOT / self.posts_path).resolve()


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from config.toml, then let env vars (DROMOS_*) override."""
    toml_values = _load_toml(config_path or DEFAULT_CONFIG_PATH)
    return Settings(**toml_values)
