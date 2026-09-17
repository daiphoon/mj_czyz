from copy import deepcopy
import json

import pytest

from sqmy.candidate_eligibility import candidate_hash, register_candidate_review, current_candidate_review
from sqmy.discovery import LiveDiscovery
from sqmy.models import Candidate, EventItem
from sqmy.screener import to_candidate
from test_discovery import FakeRouter
from test_fact_first_discovery import settings


def event():
    return EventItem(id="e1", source_id="s", source_name="调查", source_level=2,
                     title="报销材料退回", url="https://example.test/1", region="全国",
                     published_at="2026-09-07", summary="材料陈述：退回后办结。待核假设：流程衔接问题。")


def assessment(status="needs_evidence"):
    return dict(status=status, reason="办结不能说明同类流程是否顺畅", decisive_unknown="退回具体原因",
                verification_entry="公开脱敏办理结论")


def test_shadow_is_in_original_call_and_frozen_cache(settings, monkeypatch):
    discovery = LiveDiscovery(settings)
    run = discovery.wf.init_run("test_fixture")
    router = FakeRouter({"selections": [{"id": "e1", "eligibility": assessment()}]})
    monkeypatch.setattr("sqmy.discovery.build_router", lambda *a, **k: router)
    ranked, _, _ = discovery._model_rank(run, [event()])
    result = to_candidate(ranked[0], 1, settings.section("scoring"), settings.section("penalties"))
    assert result.eligibility["status"] == "needs_evidence"
    assert result.eligibility["assessed_by"] == "screening_model"
    assert result.eligibility["material_sha256"]
    assert "eligibility" in router.schema["properties"]["selections"]["items"]["properties"]
    settings.raw["candidate_eligibility"]["mode"] = "off"
    settings.raw["scoring"]["timeliness"] = 999  # 冻结后的新设置不能改变同一行为的提示。
    monkeypatch.setattr(discovery, "_approved_history", lambda: [{"title": "后来的研究"}])
    again, cached, _ = discovery._model_rank(run, [event()])
    assert cached and router.calls == 1
    assert again[0].model_analysis["_eligibility"] == ranked[0].model_analysis["_eligibility"]


def test_shadow_reject_and_missing_fields_do_not_change_old_scores(settings):
    discovery = LiveDiscovery(settings)
    run = discovery.wf.init_run("test_fixture")
    old = discovery.wf.scan(run)[0]
    assert old.eligibility is None
    item = event()
    baseline = to_candidate(item, 1, settings.section("scoring"), settings.section("penalties"))
    item.model_analysis = {"_eligibility": {"mode": "shadow", **assessment("reject")}}
    shadow = to_candidate(item, 1, settings.section("scoring"), settings.section("penalties"))
    assert (shadow.id, shadow.score, shadow.priority) == (baseline.id, baseline.score, baseline.priority)
    assert Candidate(**{k: v for k, v in old.__dict__.items() if k != "eligibility"}).eligibility is None


def test_missing_shadow_is_unavailable_and_does_not_retry(settings, monkeypatch):
    discovery = LiveDiscovery(settings)
    run = discovery.wf.init_run("test_fixture")
    router = FakeRouter({"selections": [{"id": "e1"}]})
    monkeypatch.setattr("sqmy.discovery.build_router", lambda *a, **k: router)
    ranked, _, _ = discovery._model_rank(run, [event()])
    assert ranked[0].model_analysis["_eligibility"]["status"] == "unavailable"
    assert router.calls == 1


def test_human_recommendation_binds_material_without_selecting_or_rescoring(settings):
    discovery = LiveDiscovery(settings)
    run = discovery.wf.init_run("test_fixture")
    candidate = discovery.wf.scan(run)[0]
    original = deepcopy(candidate)
    record = dict(candidate_sha256=candidate_hash(candidate), reviewer="fixture-reviewer",
                  reviewed_at="2026-09-07T12:00:00+08:00", status="needs_evidence",
                  recommendation="lead_only", facts="待核材料", current_mechanism="需核对",
                  historical_increment="新办理过程", public_value="减少重复提交",
                  authority_path="核对有权部门", decisive_unknown="办理原因",
                  verification_entry="公开办理结论", investment_reason="先复用已读材料",
                  source_refs=[{"url": "https://example.test/1", "locator": "结果段", "excerpt": "已办结"}])
    path = settings.root / "review.json"
    path.write_text(json.dumps(record, ensure_ascii=False))
    register_candidate_review(settings, run, candidate.id, path)
    register_candidate_review(settings, run, candidate.id, path)
    assert current_candidate_review(discovery.wf.db, run, candidate)["status"] == "needs_evidence"
    assert discovery.wf.candidates(run)[0] == original
    with discovery.wf.db.connect() as conn:
        assert conn.execute("SELECT SUM(selected) FROM candidates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='candidate_review'").fetchone()[0] == 1
    candidate.summary += "新事实"
    assert current_candidate_review(discovery.wf.db, run, candidate) is None
    record["candidate_sha256"] = "stale"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="版本"):
        register_candidate_review(settings, run, candidate.id, path)
