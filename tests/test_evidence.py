import json
from pathlib import Path
import tempfile
import unittest

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
        payload = {
            "topic_id": "test-minors",
            "sources": [
                {"key": "official", "source_name": "政府", "page_title": "法规", "url": "https://example.test/law", "content_hash": "law"},
                {"key": "media_a", "source_name": "媒体甲", "page_title": "发布会报道甲", "url": "https://example.test/a", "content_hash": "a"},
                {"key": "media_b", "source_name": "媒体乙", "page_title": "发布会报道乙", "url": "https://example.test/b", "content_hash": "b"},
            ],
            "claims": claims,
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


if __name__ == "__main__":
    unittest.main()
