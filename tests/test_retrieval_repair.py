from copy import deepcopy
import json

import pytest

from sqmy.research_gate import check_pre_research, review_pre_research
from sqmy.retrieval import execute_retrieval
from sqmy.tavily import TavilyClient, retrieval_usage
from test_research_gate import prepare_run, valid_brief
from test_evidence_safety import settings_at


def setup(tmp_path, monkeypatch):
    settings = settings_at(tmp_path)
    # 该组测试继续检查密钥模式旧账本；网络发送均由下方stub提供。
    settings.raw['search'].update(enabled=False, allow_paid=True)
    wf, run = prepare_run(settings)
    monkeypatch.setenv("TAVILY_API_KEY", "offline-test-key")
    calls = []
    def send(self, endpoint, payload):
        calls.append(payload)
        return {"results": [], "usage": {"credits": 2}}
    monkeypatch.setattr(TavilyClient, "_post", send)
    payload = valid_brief(run)
    payload["critical_unknowns"][0]["blocking"] = True
    payload.update(decision="stop", decision_reason="insufficient_evidence")
    file = tmp_path / "brief.json"
    file.write_text(json.dumps(payload))
    review = check_pre_research(settings, run, "C1", file)
    base = {"stage": "pre_research", "candidate_id": "C1",
            "queries": [{"purpose": "policy", "query": "现行规则"}]}
    return settings, wf, run, base, review, calls


def repair(base, review, round=1):
    plan = deepcopy(base)
    plan["queries"][0]["query"] = f"具体流程与例外 {round}"
    plan["repair"] = {"round": round, "review_id": review["review_id"],
                      "unknown_indexes": [0], "reason": "核对尚缺的前后端材料要求，决定缩题或维持停止"}
    return plan


def test_repair_uses_same_action_budget_and_does_not_release_stop(tmp_path, monkeypatch):
    settings, wf, run, base, review, calls = setup(tmp_path, monkeypatch)
    execute_retrieval(settings, wf.db, run, base)
    plan = repair(base, review)
    result = execute_retrieval(settings, wf.db, run, plan)
    assert result["status"] == "completed"
    assert execute_retrieval(settings, wf.db, run, plan)["reused"]
    execute_retrieval(settings, wf.db, run, repair(base, review, 2))
    assert len(calls) == 3
    usage = retrieval_usage(wf.db, run)["actions"]
    assert len(usage) == 1 and usage[0]["action"] == "pre_research:C1"
    assert usage[0]["accounted_credits"] == 6
    with wf.db.connect() as c:
        row = c.execute("SELECT decision,research_allowed,human_decision FROM research_reviews").fetchone()
        assert tuple(row) == ("stop", 0, None)
        assert c.execute("SELECT COUNT(*) FROM tasks WHERE kind LIKE 'retrieval:%'").fetchone()[0] == 3
    with pytest.raises(ValueError, match="轮数"):
        execute_retrieval(settings, wf.db, run, repair(base, review, 3))


@pytest.mark.parametrize("field,value", [("review_id", "old"), ("unknown_indexes", [999]),
                                         ("unknown_indexes", [0, 0]), ("reason", "")])
def test_repair_requires_current_specific_unknown(tmp_path, monkeypatch, field, value):
    settings, wf, run, base, review, calls = setup(tmp_path, monkeypatch)
    execute_retrieval(settings, wf.db, run, base)
    plan = repair(base, review)
    plan["repair"][field] = value
    with pytest.raises(ValueError):
        execute_retrieval(settings, wf.db, run, plan)
    assert len(calls) == 1


def test_repair_cannot_reset_credits_or_resume_human_stop(tmp_path, monkeypatch):
    settings, wf, run, base, review, calls = setup(tmp_path, monkeypatch)
    settings.raw["tavily"]["action_credit_limit"] = 2
    execute_retrieval(settings, wf.db, run, base)
    result = execute_retrieval(settings, wf.db, run, repair(base, review))
    assert result["status"] == "paused_budget" and len(calls) == 1
    review_pre_research(settings, run, "C1", decision="stop", note="明确结束此题")
    with pytest.raises(ValueError, match="停止"):
        execute_retrieval(settings, wf.db, run, repair(base, review))


def test_repair_cannot_change_saved_round_or_bypass_search_count(tmp_path, monkeypatch):
    settings, wf, run, base, review, calls = setup(tmp_path, monkeypatch)
    settings.raw["tavily"]["max_searches_per_action"] = 2
    execute_retrieval(settings, wf.db, run, base)
    plan = repair(base, review)
    execute_retrieval(settings, wf.db, run, plan)
    changed = deepcopy(plan)
    changed["queries"][0]["query"] = "偷换原问题单"
    with pytest.raises(ValueError, match="已固定"):
        execute_retrieval(settings, wf.db, run, changed)
    result = execute_retrieval(settings, wf.db, run, repair(base, review, 2))
    assert result["status"] == "paused_budget" and len(calls) == 2


def test_repair_can_be_disabled_without_deleting_records(tmp_path, monkeypatch):
    settings, wf, run, base, review, calls = setup(tmp_path, monkeypatch)
    execute_retrieval(settings, wf.db, run, base)
    settings.raw["tavily"]["max_repair_rounds"] = 0
    with pytest.raises(ValueError, match="关闭"):
        execute_retrieval(settings, wf.db, run, repair(base, review))
    assert execute_retrieval(settings, wf.db, run, base)["reused"]
    assert len(calls) == 1
