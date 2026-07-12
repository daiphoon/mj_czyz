from datetime import datetime, timezone
from copy import deepcopy
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from sqmy.collector import SourceCollector, canonical_url
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery
from sqmy.models import EventItem
from sqmy.providers import ModelResult


class FakeRouter:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def analyze(self, prompt, schema):
        self.calls += 1
        return ModelResult(self.data, 100, 20, "fake", "codex_cli", "test"), None


class DiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.settings = Settings.load(cls.root / "config/settings.toml")

    def test_unwraps_search_redirect(self):
        url = "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.beijing.gov.cn%2Fpolicy%3Futm_source%3Dx"
        self.assertEqual(canonical_url(url), "https://www.beijing.gov.cn/policy")

    def test_offline_live_discovery(self):
        items = []
        words = ["人工智能", "就业", "教育", "养老", "社区", "中小企业"]
        for index in range(18):
            word = words[index % len(words)]
            items.append(
                f"<item><title>北京市{word}政策机制测试信息{index}</title>"
                f"<link>https://example.gov.cn/item/{index}</link>"
                f"<description>公开数据反映相关群体存在政策执行、申诉纠错和公共服务问题。</description>"
                f"<pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate></item>"
            )
        payload = [{
            "source": {"id": "fixture", "name": "离线一级信源", "level": 1, "region": "北京", "type": "rss_search", "query": "fixture"},
            "xml": "<?xml version='1.0'?><rss><channel>" + "".join(items) + "</channel></rss>",
        }]
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "mock"
            settings = Settings(test_root, raw)
            fixture = test_root / "fixture.json"
            fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            run_id, candidates = LiveDiscovery(settings).run(fixture)
            self.assertEqual(len(candidates), 5)
            self.assertTrue((test_root / "outputs/candidates" / f"{run_id}.md").exists())
            self.assertTrue(all(c.score_reasons["来源URL"].startswith("https://") for c in candidates))

    def test_live_run_excludes_unchanged_previous_event(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            settings = Settings(test_root, deepcopy(self.settings.raw))
            discovery = LiveDiscovery(settings)
            prior = discovery.wf.init_run("live")
            event = EventItem(
                id="same", source_id="s", source_name="s", source_level=1,
                title="北京市某政策机制", url="https://example.test/same",
                published_at=datetime.now(timezone.utc).isoformat(), summary="内容", region="北京",
            )
            discovery._persist_events(prior, [event])
            current = discovery.wf.init_run("live")
            fresh, excluded = discovery._exclude_unchanged([event], current, "live")
            self.assertEqual(fresh, [])
            self.assertEqual(excluded, 1)

    def test_identical_screening_input_reuses_completed_result(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            event = EventItem(
                id="cache", source_id="s", source_name="s", source_level=1,
                title="北京市某项政策执行问题", url="https://example.test/cache",
                published_at=datetime.now(timezone.utc).isoformat(), summary="公开数据显示存在执行问题", region="北京",
                topics=["社区治理"], rule_score=70,
            )
            router = FakeRouter({"selections": [{"id": "cache"}]})
            with patch("sqmy.discovery.build_router", return_value=router):
                first_run = discovery.wf.init_run("test_fixture")
                first, first_hit, _ = discovery._model_rank(first_run, [event])
                second_run = discovery.wf.init_run("test_fixture")
                second, second_hit, saved = discovery._model_rank(second_run, [event])
            self.assertEqual([x.id for x in first], ["cache"])
            self.assertEqual([x.id for x in second], ["cache"])
            self.assertFalse(first_hit)
            self.assertTrue(second_hit)
            self.assertEqual(saved, 120)
            self.assertEqual(router.calls, 1)
