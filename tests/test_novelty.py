from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from sqmy.config import Settings
from sqmy.db import Database, now
from sqmy.models import EventItem
from sqmy.novelty import (
    NoveltyAuditor,
    production_funnel,
    record_pre_research_feedback,
    record_review,
    rolling_evaluation,
)


class NoveltyAuditTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        project = Path(__file__).parents[1]
        (self.root / "config").mkdir()
        shutil.copy(project / "config/sources.toml", self.root / "config/sources.toml")
        shutil.copy(project / "config/policy_mechanisms.toml", self.root / "config/policy_mechanisms.toml")
        base = Settings.load(project / "config/settings.toml")
        self.settings = Settings(self.root, base.raw)
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        with self.db.connect() as conn:
            stamp = now()
            conn.execute(
                "INSERT INTO runs(id,phase,status,config_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("run-1", "discovery", "running", "test", stamp, stamp),
            )
            conn.execute("INSERT INTO run_context(run_id,mode,created_at) VALUES(?,?,?)", ("run-1", "live", stamp))

    def tearDown(self):
        self.tempdir.cleanup()

    def event(self, gap: str, gap_type: str) -> EventItem:
        item = EventItem(
            id="minor-event", source_id="test", source_name="test", source_level=1,
            title="北京未成年人网络纠纷与平台规则",
            url="https://example.test/minor", published_at=datetime.now(timezone.utc).isoformat(),
            summary="研究司法个案如何触发平台规则纠错。", region="北京",
        )
        item.model_analysis = {"gap_hypothesis": gap, "gap_type": gap_type, "counter_queries": []}
        return item

    def test_curated_mechanism_blocks_direct_absence_claim(self):
        event = self.event("北京尚未建立未成年人网络纠纷向平台规则反馈的机制", "policy_absence")
        audit = NoveltyAuditor(self.settings).audit("run-1", [event], live_search=False)[0]
        self.assertEqual(audit.coverage_status, "covered")
        self.assertEqual(audit.decision, "block_original_gap")
        self.assertTrue(audit.policy_matches)

    def test_generic_platform_terms_do_not_match_unrelated_minor_policy(self):
        event = EventItem(
            id="invoice-event", source_id="test", source_name="test", source_level=2,
            title="平台消费索取发票需要跨主体逐一投诉举报",
            url="https://example.test/invoice", published_at=datetime.now(timezone.utc).isoformat(),
            summary="研究平台订单、税务投诉和商家信息协同规则。", region="北京",
        )
        event.model_analysis = {
            "gap_hypothesis": "平台索票缺少订单级协同处理机制",
            "gap_type": "coordination_gap",
            "counter_queries": [],
        }

        audit = NoveltyAuditor(self.settings).audit(
            "run-1", [event], live_search=False
        )[0]

        self.assertEqual(audit.policy_matches, [])
        self.assertEqual(audit.coverage_status, "unclear")
        self.assertEqual(audit.decision, "proceed_limited_research")

    def test_counterevidence_query_is_limited_to_official_domains(self):
        auditor = NoveltyAuditor(self.settings)
        query = auditor._official_query("北京未成年人网络保护机制")
        self.assertIn("site:beijing.gov.cn", query)
        self.assertIn("site:court.gov.cn", query)

    def test_district_query_adds_city_level_policy_search(self):
        event = EventItem(
            id="pilot-event", source_id="test", source_name="test", source_level=2,
            title="海淀智能制造新项目最高拟支持1亿元",
            url="https://example.test/pilot", published_at=datetime.now(timezone.utc).isoformat(),
            summary="海淀智谷中试港拟推出智能制造支持举措。", region="海淀",
        )
        event.model_analysis = {
            "gap_hypothesis": "海淀智能制造项目可能缺少绩效核验机制",
            "gap_type": "implementation_gap",
            "counter_queries": ["site:bjhd.gov.cn 海淀 智能制造 1亿元 支持政策"],
        }

        class FakeCollector:
            def __init__(self):
                self.calls = []

            def search_query(self, query, *, limit, lookback_days):
                self.calls.append((query, limit, lookback_days))
                if "site:beijing.gov.cn" not in query:
                    return []
                return [EventItem(
                    id="city-policy", source_id="official", source_name="official", source_level=1,
                    title="北京市印发进一步提升本市中试服务能力若干措施",
                    url="https://fgw.beijing.gov.cn/policy/pilot-platform",
                    published_at="2026-01-04T00:00:00+00:00",
                    summary="政策明确智能制造项目最高支持1亿元，并实施考核评估和结果应用。",
                    region="北京",
                )]

        auditor = NoveltyAuditor(self.settings)
        auditor.collector = FakeCollector()
        audit = auditor.audit("run-1", [event], live_search=True)[0]

        self.assertEqual(len(auditor.collector.calls), 2)
        self.assertIn("site:bjhd.gov.cn", auditor.collector.calls[0][0])
        self.assertIn("site:beijing.gov.cn", auditor.collector.calls[1][0])
        self.assertEqual(auditor.collector.calls[1][2], 730)
        self.assertEqual(audit.coverage_status, "likely_covered")
        self.assertEqual(audit.decision, "keep_with_novelty_warning")
        self.assertEqual(len(audit.counterevidence), 1)

    def test_existing_mechanism_does_not_block_effectiveness_gap(self):
        event = self.event("现有未成年人网络纠纷向平台规则反馈后缺少效果核验", "effectiveness_gap")
        audit = NoveltyAuditor(self.settings).audit("run-1", [event], live_search=False)[0]
        self.assertEqual(audit.coverage_status, "likely_covered")
        self.assertEqual(audit.decision, "keep_with_novelty_warning")

    def test_stale_mechanism_is_only_a_review_lead(self):
        auditor = NoveltyAuditor(self.settings)
        with self.db.connect() as conn:
            conn.execute("UPDATE policy_mechanisms SET last_verified_at='2020-01-01'")
        event = self.event("北京尚未建立未成年人网络纠纷向平台规则反馈的机制", "policy_absence")
        audit = auditor.audit("run-1", [event], live_search=False)[0]
        self.assertEqual(audit.coverage_status, "likely_covered")
        self.assertEqual(audit.decision, "keep_with_novelty_warning")

    def test_rolling_report_exposes_sample_and_review_limits(self):
        event = self.event("北京尚未建立未成年人网络纠纷向平台规则反馈的机制", "policy_absence")
        NoveltyAuditor(self.settings).audit("run-1", [event], live_search=False)
        result = rolling_evaluation(self.settings)
        self.assertEqual(result["automatic_conclusion"], "insufficient_sample")
        self.assertEqual(result["blocked_count"], 1)
        record_review(self.settings, "run-1:minor-event", "reversed", "人工发现机制并不覆盖该场景")
        reviewed = rolling_evaluation(self.settings)
        self.assertEqual(reviewed["reversed_block_count"], 1)

    def test_pre_research_policy_coverage_feedback_marks_early_miss(self):
        event = EventItem(
            id="elder-event", source_id="test", source_name="test", source_level=1,
            title="北京养老服务支付规则执行观察",
            url="https://example.test/elder", published_at=datetime.now(timezone.utc).isoformat(),
            summary="公开信息提示支付衔接问题。", region="北京",
        )
        event.model_analysis = {
            "gap_hypothesis": "现有支付规则的覆盖范围需要核对",
            "gap_type": "effectiveness_gap",
            "counter_queries": [],
        }
        audit = NoveltyAuditor(self.settings).audit("run-1", [event], live_search=False)[0]
        self.assertEqual(audit.coverage_status, "unclear")
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO candidates(id,run_id,title,data_json,score,selected,created_at)
                   VALUES(?,?,?,?,?,1,?)""",
                (
                    "run-1:C1", "run-1", "养老服务支付规则",
                    json.dumps({"score_reasons": {"事件ID": "elder-event"}}, ensure_ascii=False),
                    60, now(),
                ),
            )
        feedback = record_pre_research_feedback(
            self.settings,
            "run-1",
            "C1",
            {"decision": "stop", "decision_reason": "policy_covered"},
            {"valid": True},
        )
        self.assertEqual(feedback["outcome"], "missed_coverage")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT review_outcome FROM novelty_audits WHERE id='run-1:elder-event'"
            ).fetchone()
        self.assertEqual(row["review_outcome"], "missed_coverage")
        metrics = rolling_evaluation(self.settings)
        self.assertEqual(metrics["early_missed_coverage_count"], 1)
        record_review(self.settings, "run-1:elder-event", "reframed", "人工复核结论")
        preserved = record_pre_research_feedback(
            self.settings,
            "run-1",
            "C1",
            {"decision": "proceed", "decision_reason": "original_gap_supported"},
            {"valid": True},
        )
        self.assertEqual(preserved["status"], "manual_review_preserved")
        with self.db.connect() as conn:
            manual = conn.execute(
                "SELECT review_outcome,review_reason FROM novelty_audits WHERE id='run-1:elder-event'"
            ).fetchone()
        self.assertEqual(manual["review_outcome"], "reframed")
        self.assertEqual(manual["review_reason"], "人工复核结论")

    def test_production_funnel_uses_natural_week_run_cohorts(self):
        stamp = now()
        checkpoint = {
            "selected": ["C1"],
            "deep_research": {"verdict": "supports_writing_with_reframe"},
            "evidence_gate": "passed",
        }
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE runs SET checkpoint_json=?,token_used=1234,estimated_cost_cny=1.25 WHERE id='run-1'",
                (json.dumps(checkpoint, ensure_ascii=False),),
            )
            conn.execute(
                """INSERT INTO candidates(id,run_id,title,data_json,score,selected,created_at)
                   VALUES('run-1:C1','run-1','候选','{}',70,1,?)""",
                (stamp,),
            )
            conn.execute(
                """INSERT INTO research_reviews(
                     id,run_id,candidate_id,topic_id,input_hash,decision,confidence,
                     research_allowed,data_json,report_path,human_decision,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "review-1", "run-1", "C1", "topic-1", "hash", "reframe", "medium",
                    1, json.dumps({"decision_reason": "reframe_required"}), "report.md", "proceed", stamp,
                ),
            )
            conn.execute(
                """INSERT INTO topics(
                     id,title,created_at,run_id,candidate_id,approval_status,actually_submitted
                   ) VALUES('topic-1','正式稿',?,'run-1','C1','approved',1)""",
                (stamp,),
            )
        result = production_funnel(self.settings, weeks=4)
        current = result["weeks"][-1]
        self.assertEqual(current["live_runs"], 1)
        self.assertEqual(current["candidate_count"], 1)
        self.assertEqual(current["selected_topic_count"], 1)
        self.assertEqual(current["pre_research_decisions"]["reframe"], 1)
        self.assertEqual(current["human_proceed_count"], 1)
        self.assertEqual(current["deep_research_supported_count"], 1)
        self.assertEqual(current["evidence_gate_passed_count"], 1)
        self.assertEqual(current["approved_count"], 1)
        self.assertEqual(current["submitted_count"], 1)
        self.assertEqual(current["recorded_token_used"], 1234)
        self.assertTrue(current["minimum_target_met"])
        self.assertFalse(current["is_closed_week"])
        self.assertFalse(result["stability_evaluation_ready"])

    def test_production_funnel_counts_evidence_gate_before_draft(self):
        checkpoint = {
            "selected": ["C1"],
            "deep_research": {
                "verdict": "supports_writing_with_reframe",
                "draft_allowed": True,
            },
            "evidence_gate": "passed",
        }
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE runs SET checkpoint_json=? WHERE id='run-1'",
                (json.dumps(checkpoint, ensure_ascii=False),),
            )

        result = production_funnel(self.settings, weeks=4)
        current = result["weeks"][-1]
        self.assertEqual(current["deep_research_supported_count"], 1)
        self.assertEqual(current["evidence_gate_passed_count"], 1)
        self.assertEqual(current["draft_count"], 0)
        self.assertEqual(current["approved_count"], 0)
        self.assertEqual(current["submitted_count"], 0)


if __name__ == "__main__":
    unittest.main()
