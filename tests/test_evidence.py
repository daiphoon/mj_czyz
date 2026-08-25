import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from sqmy.config import Settings
from sqmy.evidence import assess_topic, import_evidence_package


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        project_root = Path(__file__).parents[1]
        base = Settings.load(project_root / "config/settings.toml")
        self.settings = Settings(self.root, base.raw)

    def tearDown(self):
        self.tempdir.cleanup()

    def package(self, claims: list[dict]) -> Path:
        checked_at = datetime.now(timezone.utc).isoformat()
        normalized_claims = []
        for claim in claims:
            item = {
                "epistemic_status": "verified_fact",
                "confidence": "high",
                "uncertainty_reason": "测试来源直接支持，仍以来源口径为限",
                "falsifier": "出现更正、撤回或相反的正式原始材料",
                "as_of_date": checked_at,
                **claim,
            }
            if item["claim_type"] == "statistic":
                item.setdefault("scope", {
                    "time_period": "测试期",
                    "region": "测试地区",
                    "population": "测试总体",
                    "unit": "项",
                    "definition": "测试定义",
                })
            normalized_claims.append(item)
        payload = {
            "topic_id": "test-minors",
            "sources": [
                {"key": "official", "source_name": "政府", "page_title": "法规", "url": "https://example.test/law", "content_hash": "law", "source_role": "official_policy", "checked_at": checked_at},
                {"key": "media_a", "source_name": "媒体甲", "page_title": "发布会报道甲", "url": "https://example.test/a", "content_hash": "a", "source_role": "media_investigation", "checked_at": checked_at},
                {"key": "media_b", "source_name": "媒体乙", "page_title": "发布会报道乙", "url": "https://example.test/b", "content_hash": "b", "source_role": "media_investigation", "checked_at": checked_at},
            ],
            "claims": normalized_claims,
        }
        path = self.root / "evidence.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_same_origin_media_reports_do_not_cross_verify(self):
        claim = {"id": "c1", "claim_text": "公布了某数据", "claim_type": "statistic", "importance": "critical", "sources": [
            {"source": "media_a", "role": "supports", "origin_group": "same-press-conference", "source_level": 2},
            {"source": "media_b", "role": "supports", "origin_group": "same-press-conference", "source_level": 2},
        ]}
        import_evidence_package(self.settings, self.package([claim]))
        result = assess_topic(self.settings, "test-minors")
        self.assertEqual(result["claims"][0]["status"], "single_source")
        self.assertFalse(result["draft_allowed"])

    def test_official_primary_source_can_stand_alone(self):
        claim = {"id": "c2", "claim_text": "法规已规定投诉机制", "claim_type": "policy", "importance": "critical", "sources": [
            {"source": "official", "role": "supports", "origin_group": "beijing-regulation", "source_level": 1, "primary_source": True},
        ]}
        import_evidence_package(self.settings, self.package([claim]))
        result = assess_topic(self.settings, "test-minors")
        self.assertEqual(result["claims"][0]["status"], "single_authoritative")
        self.assertTrue(result["draft_allowed"])

    def test_no_claims_fail_closed(self):
        import_evidence_package(self.settings, self.package([]))
        result = assess_topic(self.settings, "test-minors")
        self.assertFalse(result["draft_allowed"])
        self.assertIn("未录入任何主张", result["gate_errors"])

    def test_topic_without_critical_claims_fail_closed(self):
        claim = {
            "id": "supporting-only",
            "claim_text": "这是一个辅助背景",
            "claim_type": "background",
            "importance": "supporting",
            "sources": [
                {
                    "source": "official",
                    "role": "supports",
                    "origin_group": "official-background",
                    "source_level": 1,
                    "primary_source": True,
                }
            ],
        }
        import_evidence_package(self.settings, self.package([claim]))
        result = assess_topic(self.settings, "test-minors")
        self.assertFalse(result["draft_allowed"])
        self.assertIn("未录入核心主张", result["gate_errors"])

    def test_multiple_official_origins_are_reported_as_cross_verified(self):
        claim = {
            "id": "two-official-origins",
            "claim_text": "两个独立机关分别公布了可相互验证的事实",
            "claim_type": "policy",
            "importance": "critical",
            "sources": [
                {
                    "source": "official",
                    "role": "supports",
                    "origin_group": "official-origin-a",
                    "source_level": 1,
                    "primary_source": True,
                },
                {
                    "source": "media_a",
                    "role": "supports",
                    "origin_group": "official-origin-b",
                    "source_level": 1,
                    "primary_source": True,
                },
            ],
        }
        import_evidence_package(self.settings, self.package([claim]))
        result = assess_topic(self.settings, "test-minors")
        self.assertEqual(result["claims"][0]["status"], "cross_verified")
        self.assertIn("2 个独立信息源链", result["claims"][0]["reasons"][0])

    def test_policy_coverage_rejects_false_novelty(self):
        claim = {"id": "c3", "claim_text": "尚无投诉与平台反馈机制", "claim_type": "novelty", "importance": "critical",
                 "novelty_required": True, "policy_coverage_status": "covered", "sources": [
            {"source": "media_a", "role": "supports", "origin_group": "research-a", "source_level": 2},
            {"source": "media_b", "role": "contradicts", "origin_group": "court-guideline", "source_level": 1, "primary_source": True},
        ]}
        import_evidence_package(self.settings, self.package([claim]))
        result = assess_topic(self.settings, "test-minors")
        self.assertEqual(result["claims"][0]["status"], "rejected")
        self.assertFalse(result["draft_allowed"])

    def test_unverified_hypothesis_is_blocked_even_with_sources(self):
        claim = {
            "id": "hypothesis",
            "claim_text": "平台可能系统性拒绝某类申请",
            "claim_type": "mechanism",
            "importance": "supporting",
            "epistemic_status": "unverified_hypothesis",
            "confidence": "low",
            "sources": [{
                "source": "official",
                "role": "supports",
                "origin_group": "official-origin",
                "source_level": 1,
                "primary_source": True,
            }],
        }
        import_evidence_package(self.settings, self.package([claim]))
        result = assess_topic(self.settings, "test-minors")
        self.assertFalse(result["draft_allowed"])
        self.assertIn("假设或分析判断不得直接进入正式正文", result["claims"][0]["reasons"])

    def test_critical_statistic_requires_complete_scope(self):
        claim = {
            "id": "bad-scope",
            "claim_text": "某类案件为100件",
            "claim_type": "statistic",
            "importance": "critical",
            "scope": {"time_period": "2026年"},
            "sources": [{
                "source": "official",
                "role": "supports",
                "origin_group": "official-data",
                "source_level": 1,
                "primary_source": True,
            }],
        }
        import_evidence_package(self.settings, self.package([claim]))
        result = assess_topic(self.settings, "test-minors")
        self.assertFalse(result["draft_allowed"])
        self.assertEqual(result["claims"][0]["status"], "needs_review")
        self.assertTrue(any("统计口径缺少" in item for item in result["claims"][0]["metadata_issues"]))


if __name__ == "__main__":
    unittest.main()
