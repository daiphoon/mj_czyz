from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from sqmy.config import Settings
from sqmy.db import Database
from sqmy.evidence import assess_topic, import_evidence_package


def settings_at(root):
    base = Settings.load(Path(__file__).parents[1] / "config/settings.toml")
    return Settings(root, deepcopy(base.raw))


def package(topic="topic-a", level=2):
    stamp = datetime.now(timezone.utc).isoformat()
    return {
        "topic_id": topic,
        "sources": [{
            "key": str(i), "source_name": "测试来源", "page_title": "测试资料",
            "url": f"https://example.test/{i}", "source_role": "media_investigation",
            "checked_at": stamp, "excerpt": topic, "used_at": topic,
        } for i in range(2)],
        "claims": [{
            "id": "c1", "claim_text": topic, "claim_type": "statistic",
            "importance": "critical", "epistemic_status": "verified_fact",
            "confidence": "high", "uncertainty_reason": "测试口径限制",
            "falsifier": "原始资料更正", "as_of_date": stamp,
            "scope": dict(time_period="2026", region="测试", population="居民",
                          unit="人", definition="测试定义"),
            "sources": [{"source": str(i), "role": "supports", "origin_group": str(i),
                         "source_level": level} for i in range(2)],
        }],
    }


def ingest(settings, payload):
    path = settings.root / "package.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    import_evidence_package(settings, path)


def test_third_tier_chains_cannot_validate_core_fact(tmp_path):
    settings = settings_at(tmp_path)
    ingest(settings, package(level=3))
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


def test_one_secondary_plus_one_third_tier_is_not_cross_verified(tmp_path):
    settings = settings_at(tmp_path)
    payload = package()
    payload["claims"][0]["sources"][1]["source_level"] = 3
    ingest(settings, payload)
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


@pytest.mark.parametrize("value", ["", "   ", None, [], {}])
def test_empty_statistic_scope_is_blocked(tmp_path, value):
    settings = settings_at(tmp_path)
    payload = package()
    payload["claims"][0]["scope"]["unit"] = value
    ingest(settings, payload)
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


def test_invalid_supporting_claim_classification_is_blocked(tmp_path):
    settings = settings_at(tmp_path)
    payload = package()
    extra = deepcopy(payload["claims"][0])
    extra.update(id="c2", importance="supporting", epistemic_status="typo")
    payload["claims"].append(extra)
    ingest(settings, payload)
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


def test_missing_verification_date_is_not_replaced_with_import_time(tmp_path):
    settings = settings_at(tmp_path)
    payload = package()
    for source in payload["sources"]:
        source.pop("checked_at")
    ingest(settings, payload)
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


@pytest.mark.parametrize("field,value", [
    ("source_role", "unknown"), ("checked_at", "2999-01-01"),
])
def test_invalid_source_metadata_is_blocked(tmp_path, field, value):
    settings = settings_at(tmp_path)
    payload = package()
    payload["sources"][0][field] = value
    ingest(settings, payload)
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


def test_one_official_primary_still_passes_with_third_tier_clue(tmp_path):
    settings = settings_at(tmp_path)
    payload = package(level=3)
    payload["claims"][0]["sources"][0].update(source_level=1, primary_source=True)
    payload["sources"][0]["source_role"] = "official_data"
    ingest(settings, payload)
    gate = assess_topic(settings, "topic-a")
    assert gate["draft_allowed"]
    assert gate["claims"][0]["status"] == "single_authoritative"


def test_legacy_shared_source_requires_reimport_for_each_topic(tmp_path):
    settings = settings_at(tmp_path)
    ingest(settings, package())
    # Simulate the legacy layout, where two different claims shared a source row.
    db = Database(settings.database_path)
    with db.connect() as conn:
        conn.execute("UPDATE sources SET source_role='media_investigation',checked_at=?", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT INTO claims(id,topic_id,claim_text,claim_type,importance,created_at) VALUES('legacy-b','topic-b','b','policy','critical',?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT INTO claim_sources(claim_id,source_id,evidence_role,origin_group,source_level,primary_source,notes) SELECT 'legacy-b',source_id,evidence_role,origin_group,source_level,primary_source,notes FROM claim_sources WHERE claim_id='c1'")
        conn.execute("DELETE FROM source_usages")
    assert not assess_topic(settings, "topic-a")["draft_allowed"]
    ingest(settings, package())
    assert assess_topic(settings, "topic-a")["draft_allowed"]
    with db.connect() as conn:
        assert conn.execute("SELECT MIN(needs_review) FROM source_usages WHERE topic_id='topic-b'").fetchone()[0] == 1


def test_same_claim_id_and_source_are_isolated_between_topics(tmp_path):
    settings = settings_at(tmp_path)
    first = package()
    ingest(settings, first)
    before = assess_topic(settings, "topic-a")
    second = package("topic-b")
    for source in second["sources"]:
        source["checked_at"] = "2000-01-01"
    ingest(settings, second)
    ingest(settings, second)
    assert assess_topic(settings, "topic-a") == before
    result = assess_topic(settings, "topic-b")
    assert result["claims"][0]["claim_id"] == "c1"
    assert result["claims"][0]["claim_text"] == "topic-b"
    assert not result["draft_allowed"]
    with Database(settings.database_path).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
        usages = conn.execute("SELECT topic_id,metadata_json FROM source_usages").fetchall()
    assert len(usages) == 4
    assert all(json.loads(row["metadata_json"])["excerpt"] == row["topic_id"] for row in usages)


def test_invalid_import_rolls_back_topic_updates(tmp_path):
    settings = settings_at(tmp_path)
    payload = package()
    ingest(settings, payload)
    before = assess_topic(settings, "topic-a")
    payload["claims"][0]["claim_text"] = "不得保存"
    payload["claims"][0]["sources"][0]["source"] = "missing"
    with pytest.raises((ValueError, KeyError)):
        ingest(settings, payload)
    assert assess_topic(settings, "topic-a") == before


def test_evidence_cli_reports_block_and_accepts_verified_reimport(tmp_path, capsys):
    from sqmy.cli import main

    config = tmp_path / "config/settings.toml"
    config.parent.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "config/settings.toml", config)
    evidence = tmp_path / "evidence.json"
    with patch("sqmy.cli.load_dotenv"):
        for level, expected in ((3, 2), (2, 0)):
            evidence.write_text(json.dumps(package(level=level)), encoding="utf-8")
            assert main(["--config", str(config), "evidence-import", str(evidence)]) == 0
            capsys.readouterr()
            assert main(["--config", str(config), "evidence-check", "topic-a"]) == expected
            report = json.loads(capsys.readouterr().out)
            assert report["draft_allowed"] is (expected == 0)
