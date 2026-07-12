from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import unittest

from sqmy.config import Settings
from sqmy.db import Database, now
from sqmy.models import EventItem
from sqmy.novelty import NoveltyAuditor, record_review, rolling_evaluation


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

    def test_counterevidence_query_is_limited_to_official_domains(self):
        auditor = NoveltyAuditor(self.settings)
        query = auditor._official_query("北京未成年人网络保护机制")
        self.assertIn("site:beijing.gov.cn", query)
        self.assertIn("site:court.gov.cn", query)

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


if __name__ == "__main__":
    unittest.main()
