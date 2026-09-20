from copy import deepcopy
import json

import pytest

from sqmy.delivery import check_draft, evidence_fingerprint, require_review
from sqmy.research_brief import register_brief, latest_brief
from sqmy.research_gate import MECHANISM_FIELDS
from test_claim_evidence_v2 import detailed_package
from test_evidence_safety import ingest
from test_delivery_quality import draft_context
from test_research_v3 import problem_card
from delivery_helpers import record_valid_review


def brief_payload(wf, run):
    problem = problem_card(["c1"])
    problem.update(actors=["办理方", "申请方"], flows=["材料由申请方向办理方提交；纠错责任待审查"])
    minimum = dict(id="O1", kind="minimum", target_mechanism_ids=["H1"], expected_effect="供测试比较",
                   cost="复用既有流程", risk="复杂对象可能仍受阻",
                   **{k: "测试说明，不代表真实机制审查" for k in MECHANISM_FIELDS})
    minimum.update(authority_basis="既有受理权限", rights_impact="不新增拒绝门槛", implementation_capacity="小范围试行",
                   outcome_indicator="减少重复退回", warning_indicator="复杂对象退出增加",
                   scenarios={k: "测试情形" for k in ("baseline", "most_likely", "adverse")})
    return dict(brief_contract="research_brief_v1", run_id=run, candidate_id="C1", topic_id="delivery-topic",
                pre_research_review_id=run + ":review", evidence_sha256=evidence_fingerprint(wf.db, "delivery-topic"),
                outcome="write", conclusion="测试研究输入，不代表实际政策结论", comparison_reason="最低干预可先试行",
                remaining_unknowns=[dict(question="试行效果", blocking=False)], problem_mechanism=problem,
                options=[dict(id="O0", kind="baseline", proposal="沿用现行安排", target_mechanism_ids=["H1"],
                              expected_effect="保持现有处理", cost="原成本", risk="原问题延续"), minimum],
                selected_option_ids=["O1"])


@pytest.fixture
def v3_context(draft_context):
    settings, wf, run, source = draft_context
    data = detailed_package(); data["topic_id"] = "delivery-topic"
    ingest(settings, data)
    with wf.db.connect() as conn:
        conn.execute("UPDATE research_reviews SET data_json=?", (json.dumps({"research_contract": "research_v3"}),))
    return settings, wf, run, source


def register(context, payload=None):
    settings, wf, run, _ = context
    path = settings.root / "brief.json"
    path.write_text(json.dumps(payload or brief_payload(wf, run), ensure_ascii=False))
    return register_brief(settings, run, "delivery-topic", path)


def test_v3_requires_brief_and_reuses_same_version(v3_context):
    settings, wf, run, source = v3_context
    assert not check_draft(settings, "delivery-topic", source)["ok"]
    first = register(v3_context)
    second = register(v3_context)
    assert first == second == latest_brief(wf.db, "delivery-topic")
    assert check_draft(settings, "delivery-topic", source)["ok"]
    with wf.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='research_brief'").fetchone()[0] == 1
        assert conn.execute("SELECT SUM(token_used) FROM stage_usage WHERE stage='deep_research'").fetchone()[0] == settings.section("budget")["deep_research_tokens"]
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["unknown_mechanism", "no_baseline", "no_minimum", "no_rights", "unresolved", "blocking", "stale_pre"])
def test_broken_research_mapping_and_premises_cannot_become_writing_input(v3_context, mutation):
    _, wf, run, _ = v3_context
    payload = brief_payload(wf, run)
    if mutation == "unknown_mechanism": payload["options"][1]["target_mechanism_ids"] = ["absent"]
    if mutation == "no_baseline": payload["options"].pop(0)
    if mutation == "no_minimum": payload["options"].pop(1)
    if mutation == "no_rights": payload["options"][1].pop("rights_impact")
    if mutation == "unresolved": payload["problem_mechanism"]["hypotheses"][0]["status"] = "unresolved"
    if mutation == "blocking": payload["remaining_unknowns"][0]["blocking"] = True
    if mutation == "stale_pre": payload["pre_research_review_id"] = "old"
    with pytest.raises(ValueError): register(v3_context, payload)


def test_brief_change_invalidates_review_and_cannot_restore_old_approval(v3_context):
    settings, wf, run, source = v3_context
    first = register(v3_context)
    record_valid_review(settings, run, "delivery-topic", source)
    require_review(settings, run, "delivery-topic", source)
    payload = brief_payload(wf, run); payload["options"][1]["cost"] = "新增复杂对象核验成本"
    second = register(v3_context, payload)
    assert first["brief_sha256"] != second["brief_sha256"]
    with pytest.raises(ValueError, match="版本已变化"):
        require_review(settings, run, "delivery-topic", source)
    with pytest.raises(ValueError, match="旧版本"):
        register(v3_context)
    with wf.db.connect() as conn:
        conn.execute("UPDATE claims SET uncertainty_reason='新限制' WHERE topic_id='delivery-topic'")
    assert "研究简报绑定的证据已变化" in check_draft(settings, "delivery-topic", source)["errors"]


def test_baseline_can_win_and_no_draft_stays_non_draft(v3_context):
    settings, wf, run, source = v3_context
    payload = brief_payload(wf, run); payload["selected_option_ids"] = ["O0"]
    register(v3_context, payload)
    assert check_draft(settings, "delivery-topic", source)["ok"]
    payload.update(outcome="no_draft", reopen_condition="新证据支持机制时重开", options=[], selected_option_ids=[])
    payload["problem_mechanism"]["working_hypothesis_ids"] = []
    payload["problem_mechanism"]["hypotheses"][0]["status"] = "weakened"
    register(v3_context, payload)
    assert "最新深研结论为不成稿" in check_draft(settings, "delivery-topic", source)["errors"]
