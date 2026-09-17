from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from test_evidence_safety import settings_at, package, ingest
from sqmy.db import Database
from sqmy.evidence import assess_topic
from sqmy.delivery import evidence_fingerprint


def detailed_package():
    result = package("v2-topic")
    result["evidence_contract"] = "evidence_v2"
    for source in result["sources"]:
        source["fetched_at"] = source["checked_at"]
    for link in result["claims"][0]["sources"]:
        link["evidence_detail"] = {
            "claim_part": "样本范围内的统计结论", "locator": "表 1 及表下注释",
            "excerpt": "仅在样本总体内记录结果。", "excerpt_kind": "verbatim",
            "support_scope": "支持样本范围内的结论", "limitation": "不支持其他地区",
            "fetch_status": "excerpt_verified", "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    return result


def old_fingerprint(db, topic):
    with db.connect() as conn:
        claims = [dict(r) for r in conn.execute("SELECT * FROM claims WHERE topic_id=? ORDER BY id", (topic,))]
        links = [dict(r) for r in conn.execute("""SELECT cs.claim_id,cs.source_id,cs.evidence_role,
             cs.origin_group,cs.source_level,cs.primary_source,cs.notes,su.metadata_json,su.needs_review
             FROM claim_sources cs JOIN claims c ON c.id=cs.claim_id
             LEFT JOIN source_usages su ON su.topic_id=c.topic_id AND su.source_id=cs.source_id
             WHERE c.topic_id=? ORDER BY cs.claim_id,cs.source_id""", (topic,))]
    for claim in claims:
        claim.pop("created_at", None)
    return hashlib.sha256(json.dumps([claims, links], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.mark.parametrize("column_already_added", [False, True])
def test_migrated_legacy_evidence_keeps_exact_fingerprint(tmp_path, column_already_added):
    settings = settings_at(tmp_path); ingest(settings, package())
    db = Database(settings.database_path)
    expected = old_fingerprint(db, "topic-a")
    with db.connect() as conn:
        if not column_already_added:
            conn.execute("ALTER TABLE claim_sources DROP COLUMN evidence_detail_json")
        # 两种起点：旧结构；列已添加但迁移尚未登记版本时中断。
        conn.execute("PRAGMA user_version=0")
    db.initialize(); db.initialize()
    assert evidence_fingerprint(db, "topic-a") == expected
    with db.connect() as conn:
        assert "evidence_detail_json" in {r[1] for r in conn.execute("PRAGMA table_info(claim_sources)")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_claim_specific_passages_round_trip_and_affect_fingerprint(tmp_path):
    settings = settings_at(tmp_path); data = detailed_package()
    second = deepcopy(data["claims"][0]); second["id"] = "c2"
    second["sources"][0]["evidence_detail"]["locator"] = "表 2"
    data["claims"].append(second)
    ingest(settings, data); db = Database(settings.database_path)
    before = evidence_fingerprint(db, "v2-topic")
    with db.connect() as conn:
        details = [json.loads(r[0]) for r in conn.execute("SELECT evidence_detail_json FROM claim_sources ORDER BY claim_id,source_id")]
    assert {x["locator"] for x in details} == {"表 1 及表下注释", "表 2"}
    assert all(x["excerpt_sha256"] for x in details)
    data["claims"][0]["sources"][0]["evidence_detail"]["limitation"] = "进一步限定样本"
    ingest(settings, data)
    assert evidence_fingerprint(db, "v2-topic") != before
    assert assess_topic(settings, "v2-topic")["evidence_contract"] == "evidence_v2"


def test_legacy_reimport_cannot_remove_v2_evidence_or_restore_old_review(tmp_path):
    settings = settings_at(tmp_path); data = detailed_package(); ingest(settings, data)
    before = evidence_fingerprint(Database(settings.database_path), "v2-topic")
    data.pop("evidence_contract")
    for link in data["claims"][0]["sources"]:
        link.pop("evidence_detail")
    with pytest.raises(ValueError, match="降级"):
        ingest(settings, data)
    assert evidence_fingerprint(Database(settings.database_path), "v2-topic") == before


def test_link_failure_cannot_borrow_another_passages_read_status(tmp_path):
    settings = settings_at(tmp_path); data = detailed_package()
    data["claims"][0]["sources"][0]["evidence_detail"]["fetch_status"] = "access_failed"
    ingest(settings, data)
    gate = assess_topic(settings, "v2-topic")
    assert not gate["draft_allowed"]
    assert any("TOOL_FAILURE" in x for x in gate["claims"][0]["metadata_issues"])


def test_future_database_version_is_rejected_without_writes(tmp_path):
    settings = settings_at(tmp_path); ingest(settings, package())
    db = Database(settings.database_path)
    with db.connect() as conn: conn.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError, match='数据库版本'):
        db.initialize()
    with db.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 99
        assert conn.execute('SELECT COUNT(*) FROM claims').fetchone()[0] == 1


def test_problem_explanations_cannot_be_silently_lost_in_evidence_import(tmp_path):
    data = detailed_package(); data['problem_mechanism'] = {'question': '不能静默忽略'}
    with pytest.raises(ValueError, match='research-brief'):
        ingest(settings_at(tmp_path), data)


@pytest.mark.parametrize("mutation", ["missing_detail", "bad_hash", "unknown_contract", "bad_date"])
def test_invalid_new_contract_is_rejected_before_evidence_changes(tmp_path, mutation):
    settings = settings_at(tmp_path); data = detailed_package()
    link = data["claims"][0]["sources"][0]
    if mutation == "missing_detail": link.pop("evidence_detail")
    if mutation == "bad_hash": link["evidence_detail"]["excerpt_sha256"] = "wrong"
    if mutation == "unknown_contract": data["evidence_contract"] = "future"
    if mutation == "bad_date": link["evidence_detail"]["checked_at"] = "yesterday"
    with pytest.raises(ValueError): ingest(settings, data)
