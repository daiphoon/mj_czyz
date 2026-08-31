from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

import pytest

from sqmy.config import Settings
from sqmy.research_gate import (
    check_pre_research,
    require_pre_research_approval,
    review_pre_research,
)
from sqmy.workflow import Workflow


def valid_brief(run_id: str) -> dict:
    checked_at = datetime.now(timezone.utc).isoformat()
    return {
        "run_id": run_id,
        "candidate_id": "C1",
        "topic_id": "minors-refund-proof",
        "working_title": "关于完善北京市未成年人网络消费退款举证与对称核验机制的建议",
        "decision": "reframe",
        "decision_reason": "reframe_required",
        "confidence": "medium",
        "confidence_reason": "现行规范和司法个案可验证，但问题规模及平台差异仍待深研",
        "sources": [
            {
                "key": "policy",
                "url": "https://example.gov.cn/policy",
                "publisher": "主管部门",
                "published_at": "2026-04-13",
                "checked_at": checked_at,
                "source_level": 1,
                "source_role": "official_policy",
                "origin_group": "policy-origin",
            },
            {
                "key": "court",
                "url": "https://example.gov.cn/court",
                "publisher": "人民法院",
                "published_at": "2025-04-15",
                "checked_at": checked_at,
                "source_level": 1,
                "source_role": "court_case",
                "origin_group": "court-origin",
            },
        ],
        "verified_facts": [{
            "statement": "现行规范已经要求平台设置未成年人退款机制",
            "source_keys": ["policy"],
        }],
        "evidence_based_inferences": [{
            "statement": "政策缺口更可能位于退款举证与前端核验不对称，而非完全没有退款渠道",
            "basis": "规范已经规定退款，司法个案显示前端限制可被低成本解除",
            "uncertainty": "尚不能从单一个案推断所有平台均存在相同机制",
            "falsifier": "平台公开流程及代表性案件证明前后端核验强度对称",
            "source_keys": ["policy", "court"],
        }],
        "unverified_hypotheses": [{
            "statement": "部分平台退款环节要求的证明强度显著高于支付或解除限制环节",
            "verification_plan": "抽取规则文本、司法案例和投诉样本比较各环节材料要求",
            "discard_if": "主要平台均已提供同等强度且可复核的前后端核验记录",
        }],
        "analyst_judgments": [{
            "statement": "将题目改写为对称核验比泛泛要求加强保护更有制度新意",
            "rationale": "既承认已有退款规则，又把责任与信息掌握关系落到可执行流程",
        }],
        "counterevidence": [{
            "statement": "现行规范已明确要求平台建立未成年人退款机制",
            "implication": "不得继续以退款制度完全缺失作为核心主张",
            "source_keys": ["policy"],
        }],
        "alternative_explanations": [{
            "statement": "争议增加可能主要来自网络消费增长，而非退款机制恶化",
            "test": "比较交易规模、纠纷率和规则变化，避免把数量增长直接解释为制度失效",
        }],
        "critical_unknowns": [{
            "question": "不同平台现行退款材料要求是否存在实质差异",
            "blocking": False,
            "resolution_plan": "深研阶段抽样核对公开规则和典型裁判文书",
        }],
        "discard_conditions": ["无法获得两个独立来源链证明对称核验缺口具有制度性"],
        "authority": {
            "actor": "北京市网信、市场监管等部门及属地互联网法院协同",
            "power": "可通过合规指引、消费争议治理和试点推动最小凭证规则",
            "boundary": "不得代替中央立法，也不得要求地方直接改写全国平台算法",
        },
        "research_questions": ["前端支付或解除限制与后端退款分别核验哪些事实和材料"],
        "budget": {
            "token_limit": 30_000,
            "reason": "只围绕对称核验、证据负担和北京权限开展定向核验",
            "expected_benefit": "尽早证伪泛化假设，避免把深研Token用于已有政策覆盖内容",
        },
        "mechanism_cards": [{
            "proposal": "建立前后端对称核验和最小凭证清单",
            "actor": "北京市相关主管部门牵头平台试点",
            "target": "涉及未成年人高风险网络消费及退款申请",
            "trigger": "异常消费、身份限制解除或监护人退款申请",
            "information": "平台掌握账户、设备、支付、限制解除和申诉日志",
            "cost_bearer": "平台承担保全和出示其控制范围内日志的成本",
            "beneficiary": "未成年人家庭、守规平台及争议处理机构",
            "expected_behavior": "平台在前端提高必要核验并保留可复核记录",
            "evasion_risk": "以形式化弹窗替代实质核验或拆分账户规避异常识别",
            "cost_transfer_risk": "过度核验可能增加普通用户摩擦并诱发一刀切拒付",
            "verification": "抽查前后端日志字段、处理时限和复议结果，不收集无关隐私",
            "correction": "提供补充材料、人工复核和错误拒绝纠正流程",
            "exit_condition": "试点未降低争议处理成本或明显增加误伤时修订或退出",
            "lower_cost_alternative": "先统一最小日志和举证责任说明，不新建平台",
            "scenarios": {
                "baseline": "各平台按现有规则分别处理，举证责任不透明",
                "most_likely": "试点降低部分争议的信息不对称和重复举证",
                "adverse": "平台转为过度限制账户或把合规成本转嫁给普通用户",
            },
        }],
    }


def make_settings(temp: str) -> Settings:
    project = Path(__file__).parents[1]
    base = Settings.load(project / "config/settings.toml")
    return Settings(Path(temp), deepcopy(base.raw))


def prepare_run(settings: Settings) -> tuple[Workflow, str]:
    workflow = Workflow(settings)
    run_id = workflow.init_run("live")
    workflow.scan(run_id)
    workflow.select(run_id, ["C1"])
    return workflow, run_id


def test_valid_reframed_brief_is_idempotent_and_stops_at_human_gate():
    with tempfile.TemporaryDirectory() as temp:
        settings = make_settings(temp)
        workflow, run_id = prepare_run(settings)
        brief = settings.root / "brief.json"
        brief.write_text(json.dumps(valid_brief(run_id), ensure_ascii=False), encoding="utf-8")
        first = check_pre_research(settings, run_id, "C1", brief)
        second = check_pre_research(settings, run_id, "C1", brief)
        assert first["input_hash"] == second["input_hash"]
        assert first["gate"]["research_allowed"] is True
        assert Path(first["report_path"]).exists()
        report = Path(first["report_path"]).read_text(encoding="utf-8")
        assert "## 关键未知" in report
        assert "## 机制压力测试" in report
        assert first["novelty_feedback"]["status"] == "not_linked"
        with workflow.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM research_reviews WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            usage = conn.execute(
                "SELECT stage,token_used,accounting_method FROM stage_usage WHERE run_id=?",
                (run_id,),
            ).fetchall()
        assert count == 1
        assert [(row["stage"], row["token_used"]) for row in usage] == [
            ("pre_research", 30_000)
        ]
        assert usage[0]["accounting_method"] == "declared_stage_cap"
        status = workflow.status(run_id)[0]
        assert status["phase"] == "research"
        assert status["status"] == "needs_review"
        with pytest.raises(ValueError, match="尚未人工确认"):
            require_pre_research_approval(settings, run_id, "C1", "minors-refund-proof")


def test_stale_selection_must_be_refreshed_before_pre_research():
    with tempfile.TemporaryDirectory() as temp:
        settings = make_settings(temp)
        workflow, run_id = prepare_run(settings)
        with workflow.db.connect() as conn:
            conn.execute(
                "UPDATE runs SET created_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(), run_id),
            )
        brief = settings.root / "brief.json"
        brief.write_text(json.dumps(valid_brief(run_id), ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="来源新鲜度"):
            check_pre_research(settings, run_id, "C1", brief)


def test_blocking_unknown_cannot_be_overridden_by_human_review():
    with tempfile.TemporaryDirectory() as temp:
        settings = make_settings(temp)
        _, run_id = prepare_run(settings)
        payload = valid_brief(run_id)
        payload["critical_unknowns"][0]["blocking"] = True
        brief = settings.root / "brief.json"
        brief.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = check_pre_research(settings, run_id, "C1", brief)
        assert result["gate"]["research_allowed"] is False
        assert any("阻断性关键未知" in item for item in result["gate"]["errors"])
        with pytest.raises(ValueError, match="不能人工放行"):
            review_pre_research(
                settings,
                run_id,
                "C1",
                decision="proceed",
                note="人工希望继续",
            )


def test_human_proceed_unlocks_only_matching_topic():
    with tempfile.TemporaryDirectory() as temp:
        settings = make_settings(temp)
        _, run_id = prepare_run(settings)
        brief = settings.root / "brief.json"
        brief.write_text(json.dumps(valid_brief(run_id), ensure_ascii=False), encoding="utf-8")
        check_pre_research(settings, run_id, "C1", brief)
        review_pre_research(
            settings,
            run_id,
            "C1",
            decision="proceed",
            note="确认按改写后的研究问题进入深研",
        )
        require_pre_research_approval(settings, run_id, "C1", "minors-refund-proof")
        check_pre_research(settings, run_id, "C1", brief)
        status = Workflow(settings).status(run_id)[0]
        assert status["status"] == "pending"
        assert "开展深研" in status["checkpoint_json"]
        with pytest.raises(ValueError, match="尚未形成"):
            require_pre_research_approval(settings, run_id, "C1", "different-topic")


def test_invalid_decision_reason_is_rejected():
    with tempfile.TemporaryDirectory() as temp:
        settings = make_settings(temp)
        _, run_id = prepare_run(settings)
        payload = valid_brief(run_id)
        payload["decision_reason"] = "free_text_reason"
        brief = settings.root / "brief.json"
        brief.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = check_pre_research(settings, run_id, "C1", brief)
        assert result["gate"]["valid"] is False
        assert any("decision_reason" in item for item in result["gate"]["errors"])
        assert result["novelty_feedback"]["status"] == "not_recorded_invalid_gate"
