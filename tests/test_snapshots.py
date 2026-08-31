from copy import deepcopy
import json
from pathlib import Path
import tempfile

import pytest

from sqmy.config import Settings
from sqmy.snapshots import capture_evidence_snapshot


ROOT = Path(__file__).parents[1]
BASE_SETTINGS = Settings.load(ROOT / "config/settings.toml")


def _package(
    path: Path,
    *,
    used_at: str | None = "正文制度缺口",
    url: str = "https://rules.example.com/current",
) -> Path:
    package = path / "evidence.json"
    package.write_text(
        json.dumps(
            {
                "topic_id": "dynamic-public-rule",
                "sources": [
                    {
                        "key": "platform_rule",
                        "source_name": "平台公开规则",
                        "page_title": "当前审核规则",
                        "url": url,
                        "excerpt": "当前公开页面说明了审核条件。",
                        "used_at": used_at,
                    }
                ],
                "claims": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return package


def test_capture_evidence_snapshot_is_small_auditable_and_idempotent():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = Settings(root, deepcopy(BASE_SETTINGS.raw))
        package = _package(root)
        page = root / "page.html"
        page.write_text("<!doctype html><html><body>规则正文</body></html>", encoding="utf-8")

        first = capture_evidence_snapshot(
            settings,
            package,
            "platform_rule",
            reason="dynamic_content",
            local_file=page,
        )
        second = capture_evidence_snapshot(
            settings,
            package,
            "platform_rule",
            reason="dynamic_content",
            local_file=page,
        )

        snapshot = root / first["snapshot"]["file_path"]
        assert snapshot.exists()
        assert first["snapshot"]["content_hash"] == second["snapshot"]["content_hash"]
        assert first["model_calls"] == 0
        manifest = json.loads(Path(first["manifest"]).read_text(encoding="utf-8"))
        assert len(manifest["snapshots"]) == 1
        assert manifest["snapshots"][0]["used_at"] == "正文制度缺口"
        assert manifest["snapshots"][0]["media_type"] == "text/html"
        assert "headers" not in manifest["snapshots"][0]


def test_capture_evidence_snapshot_rejects_source_not_used_in_formal_research():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = Settings(root, deepcopy(BASE_SETTINGS.raw))
        package = _package(root, used_at=None)
        page = root / "page.html"
        page.write_text("<html><body>普通线索</body></html>", encoding="utf-8")

        with pytest.raises(ValueError, match="核心来源"):
            capture_evidence_snapshot(
                settings,
                package,
                "platform_rule",
                reason="dynamic_content",
                local_file=page,
            )


def test_capture_evidence_snapshot_rejects_credentials_in_source_url():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = Settings(root, deepcopy(BASE_SETTINGS.raw))
        package = _package(root, url="https://rules.example.com/current?access_token=secret")
        page = root / "page.html"
        page.write_text("<html><body>公开规则</body></html>", encoding="utf-8")

        with pytest.raises(ValueError, match="敏感查询参数"):
            capture_evidence_snapshot(
                settings,
                package,
                "platform_rule",
                reason="dynamic_content",
                local_file=page,
            )
