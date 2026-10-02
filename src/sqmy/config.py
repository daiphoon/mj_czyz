from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import os
import re
import tomllib


def validate_selected_model(value: str | None) -> str:
    if value is None or value == "":
        raise ValueError("未指定本次模型；程序不能自动读取Codex界面选择。请在命令前传入 --model MODEL_ID。")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise ValueError("--model 须填写完整模型标识，不能包含空格或命令参数。")
    return value


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
    selected_model: str | None = None

    @classmethod
    def load(cls, path: str | Path = "config/settings.toml") -> "Settings":
        config_path = Path(path).resolve()
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)
        return cls(config_path.parent.parent, raw)

    def section(self, name: str) -> dict:
        return self.raw[name]

    def with_model(self, model: str) -> "Settings":
        return replace(self, selected_model=validate_selected_model(model))

    def require_model(self) -> str:
        return validate_selected_model(self.selected_model)

    @property
    def interactive_model(self) -> str:
        # 未传入的交互产物只登记未知，不用旧配置冒充会话实际模型。
        return self.selected_model or "unknown"

    @property
    def database_path(self) -> Path:
        return self.root / "data/history/workflow.db"

    @property
    def reference_document(self) -> Path | None:
        value = os.environ.get("SQMY_REFERENCE_PATH") or self.section("document").get("reference_path")
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path
