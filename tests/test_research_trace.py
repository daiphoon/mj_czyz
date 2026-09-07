from copy import deepcopy
import json

import pytest

from sqmy.evidence import assess_topic
from sqmy.research_gate import (
    check_pre_research, require_pre_research_approval, review_pre_research,
    validate_pre_research_payload, _render_report,
)
from test_evidence_safety import ingest, package, settings_at
from test_research_gate import valid_brief, prepare_run


@pytest.mark.parametrize("status", ["summary_only", "access_failed", "irrelevant"])
def test_unread_source_cannot_support_formal_fact(tmp_path, status):
    settings = settings_at(tmp_path)
    payload = package()
    for source in payload["sources"]:
        source["fetch_status"] = status
    ingest(settings, payload)
    gate = assess_topic(settings, "topic-a")
    assert not gate["draft_allowed"]
    assert any(status in reason for reason in gate["claims"][0]["reasons"])


def test_url_and_source_labels_without_passage_cannot_validate(tmp_path):
    settings = settings_at(tmp_path)
    payload = package()
    for source in payload["sources"]:
        source.pop("excerpt")
    ingest(settings, payload)
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


def test_unsupported_supporting_fact_blocks_writing(tmp_path):
    settings = settings_at(tmp_path)
    payload = package()
    extra = deepcopy(payload["claims"][0])
    extra.update(id="unverified-background", importance="supporting", sources=[])
    payload["claims"].append(extra)
    ingest(settings, payload)
    assert not assess_topic(settings, "topic-a")["draft_allowed"]


def test_attributed_single_source_background_is_not_upgraded_to_core_threshold(tmp_path):
    settings = settings_at(tmp_path)
    payload = package()
    extra = deepcopy(payload["claims"][0])
    extra.update(id="attributed-background", importance="supporting", claim_type="comparative_policy",
                 claim_text="这一来源公布了自身规则，不证明实际实施效果。")
    extra["sources"] = extra["sources"][:1]
    payload["claims"].append(extra)
    ingest(settings, payload)
    assert assess_topic(settings, "topic-a")["draft_allowed"]


def test_report_carries_passage_limit_and_unread_diagnostics(tmp_path):
    settings = settings_at(tmp_path)
    payload = valid_brief("run")
    for source in payload["sources"]:
        source.update(excerpt="已查处一案", limitation="不能推算全国发生率",
                      locator="通报第二段", fetch_status="access_failed")
    gate = validate_pre_research_payload(settings, payload)
    assert gate["record_valid"]  # 技术失败不等于记录格式损坏或事实为假。
    assert not gate["research_allowed"]
    assert "TOOL_FAILURE" in gate["diagnostics"]["reason_codes"]
    assert "FACT_FALSE" not in gate["diagnostics"]["reason_codes"]
    report = _render_report(payload, gate)
    assert "已查处一案" in report and "不能推算全国发生率" in report
    assert "通报第二段" in report and "access_failed" in report


def test_reframed_topic_cannot_reuse_previous_topic_approval(tmp_path):
    settings = settings_at(tmp_path)
    _, run = prepare_run(settings)
    payload = valid_brief(run)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(payload))
    check_pre_research(settings, run, "C1", path)
    review_pre_research(settings, run, "C1", decision="proceed", note="人工确认原切口")
    old_topic = payload["topic_id"]
    payload.update(topic_id="reframed-topic", working_title="改变核心命题的新切口")
    path.write_text(json.dumps(payload))
    check_pre_research(settings, run, "C1", path)
    with pytest.raises(ValueError, match="最新|版本|题目"):
        require_pre_research_approval(settings, run, "C1", old_topic)


def test_reimport_old_brief_cannot_restore_old_approval(tmp_path):
    settings = settings_at(tmp_path)
    _, run = prepare_run(settings)
    payload = valid_brief(run)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(payload))
    check_pre_research(settings, run, "C1", path)
    review_pre_research(settings, run, "C1", decision="proceed", note="人工确认")
    changed = deepcopy(payload)
    changed["critical_unknowns"][0]["blocking"] = True
    path.write_text(json.dumps(changed))
    check_pre_research(settings, run, "C1", path)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="旧版本|最新"):
        check_pre_research(settings, run, "C1", path)


def test_preflight_cannot_find_older_proceed_after_new_stop(tmp_path):
    from sqmy.maintenance import _bounded_stage_context
    settings = settings_at(tmp_path)
    wf, run = prepare_run(settings)
    payload = valid_brief(run)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(payload))
    check_pre_research(settings, run, "C1", path)
    review_pre_research(settings, run, "C1", decision="proceed", note="人工确认")
    payload.update(topic_id="changed", decision="stop", decision_reason="insufficient_evidence")
    path.write_text(json.dumps(payload))
    check_pre_research(settings, run, "C1", path)
    assert not _bounded_stage_context(settings, wf.db, run, "research")[0]


def test_fact_false_cannot_be_compensated_by_proceed(tmp_path):
    payload = valid_brief("run")
    payload.update(decision="proceed", decision_reason="fact_contradicted")
    payload["critical_unknowns"][0]["blocking"] = True
    gate = validate_pre_research_payload(settings_at(tmp_path), payload)
    assert gate["record_valid"] and not gate["research_allowed"]
    assert "FACT_FALSE" in gate["diagnostics"]["reason_codes"]
    assert gate["diagnostics"]["disposition"] == "rejected_cut"


def test_fulltext_declaration_requires_locator_and_known_references(tmp_path):
    payload = valid_brief("run")
    payload["sources"][0]["fetch_status"] = "fulltext_ok"
    gate = validate_pre_research_payload(settings_at(tmp_path), payload)
    assert not gate["research_allowed"]
    payload["sources"][0]["locator"] = "第二条"
    assert validate_pre_research_payload(settings_at(tmp_path), payload)["research_allowed"]
    payload["verified_facts"][0]["source_keys"] = ["made-up"]
    assert not validate_pre_research_payload(settings_at(tmp_path), payload)["record_valid"]


def test_legacy_stop_without_reason_is_not_relabelled_insufficient_evidence(tmp_path):
    payload = valid_brief("run")
    payload["decision"] = "stop"
    payload.pop("decision_reason")
    gate = validate_pre_research_payload(settings_at(tmp_path), payload)
    assert gate["diagnostics"]["disposition"] == "unclassified"
    assert "GAP_UNPROVEN" not in gate["diagnostics"]["reason_codes"]
