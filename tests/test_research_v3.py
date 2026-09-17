from copy import deepcopy
import json

import pytest

from test_research_gate import valid_brief, make_settings, prepare_run
from sqmy.research_gate import validate_pre_research_payload, check_pre_research, review_pre_research


def problem_card(claim_ids=None):
    refs = claim_ids or ["F1", "I1"]
    return {
        "route": "observed_problem", "question": "哪个环节的证据要求产生了具体差异？",
        "process_boundary": "支付至退款", "process_step": "退款核验",
        "baseline": "沿用现行处理规则", "policy_maturation": "需核对新规则的适用和实施时间",
        "working_hypothesis_ids": ["H1"],
        "hypotheses": [{
            "id": key, "statement": statement, "prediction": "若解释成立，应观察到相应办理记录",
            "supporting_claim_ids": refs if key == "H1" else [], "counter_claim_ids": [],
            "discriminating_test": "比较同类办理流程和结果", "falsifier": "有适用且相反的办理材料",
            "status": "supported" if key == "H1" else "unresolved",
        } for key, statement in [("H1", "前后端信息核验要求不一致"), ("H2", "差异来自风险及个案条件")]],
    }


def v3_brief(run):
    result = valid_brief(run); result["research_contract"] = "research_v3"
    for key, prefix in [("verified_facts", "F"), ("evidence_based_inferences", "I"), ("counterevidence", "C")]:
        for i, item in enumerate(result[key], 1): item["id"] = f"{prefix}{i}"
    result["problem_mechanism"] = problem_card()
    result["initial_feasibility"] = {"authority": "沿用已核权限", "major_risks": "先核对是否扩大个人信息收集",
                                     "lower_cost_alternative": "先说明现行举证规则"}
    result.pop("mechanism_cards"); result.pop("alternative_explanations")
    return result


def stopped_brief(run):
    result = v3_brief(run)
    result.update(decision="stop", decision_reason="tool_failure", sources=[], verified_facts=[],
                  evidence_based_inferences=[], counterevidence=[], unverified_hypotheses=[], analyst_judgments=[],
                  stop_summary="决定性页面无法取得，真实性尚未完成核验", reopen_condition="取得对应的可读公开材料后重新评估",
                  effort_note="一次本地缓存查找，没有模型或搜索请求", research_questions=[])
    result.pop("problem_mechanism"); result.pop("initial_feasibility"); result.pop("authority")
    return result


def test_technical_stop_can_be_complete_without_fabricated_facts_or_proposals(tmp_path):
    settings = make_settings(str(tmp_path))
    gate = validate_pre_research_payload(settings, stopped_brief("r"))
    assert gate["record_valid"]
    assert not gate["research_allowed"]
    assert gate["diagnostics"]["disposition"] == "tech_blocked"


def test_v3_proceed_needs_problem_explanations_not_full_intervention_cards(tmp_path):
    settings = make_settings(str(tmp_path)); brief = v3_brief("r")
    assert validate_pre_research_payload(settings, brief)["research_allowed"]
    brief["critical_unknowns"][0]["blocking"] = True
    assert not validate_pre_research_payload(settings, brief)["research_allowed"]


def test_new_candidate_cannot_drop_research_contract(tmp_path):
    settings = make_settings(str(tmp_path)); wf, run = prepare_run(settings)
    with wf.db.connect() as conn:
        row = conn.execute("SELECT data_json FROM candidates WHERE id=?", (run + ':C1',)).fetchone()
        data = json.loads(row[0]); data['eligibility'] = {'contract': 'candidate_shadow_v1', 'status': 'unavailable'}
        conn.execute('UPDATE candidates SET data_json=? WHERE id=?', (json.dumps(data), run + ':C1'))
    path = settings.root / 'legacy.json'; path.write_text(json.dumps(valid_brief(run)))
    with pytest.raises(ValueError, match='research_v3'):
        check_pre_research(settings, run, 'C1', path)


@pytest.mark.parametrize("mutation", ["unknown_ref", "missing_test", "duplicate_id", "missing_problem", "unclassified_reason"])
def test_problem_card_does_not_pass_with_broken_references_or_empty_reasoning(tmp_path, mutation):
    settings = make_settings(str(tmp_path)); brief = v3_brief("r")
    if mutation == "unknown_ref": brief["problem_mechanism"]["hypotheses"][0]["supporting_claim_ids"] = ["fake"]
    if mutation == "missing_test": brief["problem_mechanism"]["hypotheses"][0]["discriminating_test"] = ""
    if mutation == "duplicate_id": brief["problem_mechanism"]["hypotheses"][1]["id"] = "H1"
    if mutation == "missing_problem": brief.pop("problem_mechanism")
    if mutation == "unclassified_reason": brief.pop("decision_reason")
    assert not validate_pre_research_payload(settings, brief)["research_allowed"]


def test_new_stop_contract_cannot_be_downgraded_or_human_overridden(tmp_path):
    settings = make_settings(str(tmp_path)); wf, run = prepare_run(settings)
    path = tmp_path / "brief.json"; path.write_text(json.dumps(stopped_brief(run)))
    result = check_pre_research(settings, run, "C1", path)
    assert result["gate"]["record_valid"]
    with pytest.raises(ValueError, match="阻断"):
        review_pre_research(settings, run, "C1", decision="proceed", note="test")
    path.write_text(json.dumps(valid_brief(run)))
    with pytest.raises(ValueError, match="降级"):
        check_pre_research(settings, run, "C1", path)
