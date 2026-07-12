from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    root: Path
    raw: dict

    @classmethod
    def load(cls, path: str | Path = "config/settings.toml") -> "Settings":
        config_path = Path(path).resolve()
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)
        return cls(config_path.parent.parent, raw)

    def section(self, name: str) -> dict:
        return self.raw[name]

    @property
    def database_path(self) -> Path:
        return self.root / "data/history/workflow.db"
