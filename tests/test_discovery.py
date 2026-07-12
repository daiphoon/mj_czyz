from datetime import datetime, timezone
from copy import deepcopy
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from sqmy.collector import SourceCollector, canonical_url, infer_event_region
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery, same_origin_event
from sqmy.models import EventItem
from sqmy.providers import ModelResult
from sqmy.screener import classify


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

    def test_event_region_does_not_use_source_channel_as_event_location(self):
        region, evidence = infer_event_region(
            "新华网民生观察丨从填表到刷脸，谁在过度收集个人信息?",
            "新华网北京7月7日电 记者在江苏苏州等地采访发现相关问题。",
            "https://www.news.cn/fortune/20260707/example.htm",
            "北京",
        )
        self.assertEqual(region, "全国")
        self.assertEqual(evidence, "source_channel_only:北京")

    def test_event_region_uses_explicit_local_evidence(self):
        self.assertEqual(
            infer_event_region(
                "北京互联网法院发布未成年人网络纠纷情况",
                "相关案件呈现新特点。",
                "https://example.test/court",
                "全国",
            )[0],
            "北京",
        )
        self.assertEqual(
            infer_event_region("社区服务调整", "通知内容", "https://www.bjhd.gov.cn/example", "全国")[0],
            "海淀",
        )

    def test_expanded_topic_scope_is_classified(self):
        event = EventItem(
            id="food", source_id="s", source_name="s", source_level=1,
            title="校园餐食品安全抽检发现流程问题", url="https://example.test/food",
            published_at="2026-07-13T00:00:00+00:00", summary="涉及未成年人公共利益",
            region="北京",
        )
        self.assertIn("食品安全", classify(event))

    def test_source_expansion_tier_is_carried_to_event(self):
        payload = [{
            "source": {"id": "national", "name": "全国来源", "level": 1, "region": "全国", "expansion_tier": 3},
            "xml": "<rss><channel><item><title>全国青年就业政策问题</title><link>https://example.gov.cn/youth</link><description>公开调查数据</description><pubDate>Mon, 13 Jul 2026 00:00:00 GMT</pubDate></item></channel></rss>",
        }]
        events = SourceCollector(self.root, self.settings.raw)._items_from_payloads(payload)
        self.assertEqual(events[0].expansion_tier, 3)

    def test_small_fresh_batch_is_deferred_without_model(self):
        discovery = LiveDiscovery(self.settings)
        events = [
            EventItem(
                id=str(index), source_id="s", source_name="s", source_level=1,
                title=f"北京市公共服务常规信息{index}", url=f"https://example.test/{index}",
                published_at="2026-07-13T00:00:00+00:00", summary="政策执行信息",
                region="北京", rule_score=80,
            )
            for index in range(2)
        ]
        self.assertEqual(discovery._should_run_model(events, force=False), (False, "deferred_small_batch"))
        self.assertEqual(discovery._should_run_model(events, force=True), (True, "forced"))
        self.assertEqual(
            discovery._should_run_model(events, force=False, final_attempt=True),
            (True, "cascade_exhausted_floor"),
        )

    def test_small_batch_urgent_exception(self):
        discovery = LiveDiscovery(self.settings)
        event = EventItem(
            id="urgent", source_id="s", source_name="s", source_level=1,
            title="北京市就重大风险发布专项通报", url="https://example.test/urgent",
            published_at="2026-07-13T00:00:00+00:00", summary="涉及紧急处置",
            region="北京", rule_score=95,
        )
        self.assertEqual(discovery._should_run_model([event], force=False), (True, "urgent_exception"))

    def test_small_fixture_finishes_with_zero_model_calls(self):
        titles = ["北京市养老服务政策执行观察", "北京市人工智能企业治理机制观察"]
        items = "".join(
            f"<item><title>{title}</title>"
            f"<link>https://example.gov.cn/small/{index}</link>"
            "<description>公开信息反映相关政策执行情况。</description>"
            f"<pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate></item>"
            for index, title in enumerate(titles)
        )
        payload = [{
            "source": {"id": "fixture", "name": "离线一级信源", "level": 1, "region": "北京", "type": "rss_search", "query": "fixture"},
            "xml": "<?xml version='1.0'?><rss><channel>" + items + "</channel></rss>",
        }]
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            settings = Settings(test_root, deepcopy(self.settings.raw))
            fixture = test_root / "fixture.json"
            fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            run_id, candidates = LiveDiscovery(settings).run(fixture)
            with LiveDiscovery(settings).wf.db.connect() as conn:
                calls = conn.execute("SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)).fetchone()[0]
                efficiency = conn.execute("SELECT model_input_count,deferred_count FROM run_efficiency WHERE run_id=?", (run_id,)).fetchone()
            self.assertEqual(candidates, [])
            self.assertEqual(calls, 0)
            self.assertEqual(tuple(efficiency), (0, 2))

    def test_detects_cross_site_same_origin_event(self):
        current = EventItem(
            id="current", source_id="s", source_name="s", source_level=2,
            title="北京互联网法院：未成年人游戏充值、直播打赏等退款纠纷占比86.4%",
            url="https://media.example/current", published_at="2026-07-08T01:00:00+00:00",
            summary="北京互联网法院通报未成年人网络纠纷情况。", region="北京",
        )
        previous = {
            "title": "涉未成年人网络纠纷5年增长近20倍 北京互联网法院发布情况",
            "summary": "北京互联网法院介绍游戏充值和直播打赏纠纷。",
            "published_at": "2026-07-08T03:00:00+00:00",
        }
        self.assertTrue(same_origin_event(current, previous))

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

    def test_tier_continuation_excludes_earlier_sources(self):
        words = ["人工智能", "就业", "教育", "养老", "社区", "中小企业", "食品安全", "住房"]
        payload = []
        for tier in (1, 2):
            items = "".join(
                f"<item><title>北京市{word}政策问题第{tier}层{index}</title>"
                f"<link>https://example.gov.cn/tier{tier}/{index}</link>"
                "<description>公开数据反映相关群体存在政策执行和公共服务问题。</description>"
                f"<pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate></item>"
                for index, word in enumerate(words)
            )
            payload.append({
                "source": {"id": f"tier{tier}", "name": f"第{tier}层来源", "level": 1, "region": "北京", "type": "rss_search", "query": "fixture", "expansion_tier": tier},
                "xml": "<?xml version='1.0'?><rss><channel>" + items + "</channel></rss>",
            })
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
            run_id, candidates = LiveDiscovery(settings).run(fixture, start_tier=2)
            with LiveDiscovery(settings).wf.db.connect() as conn:
                tiers = {row[0] for row in conn.execute("SELECT expansion_tier FROM event_items WHERE run_id=?", (run_id,))}
                checkpoint = json.loads(conn.execute("SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            self.assertEqual(len(candidates), 5)
            self.assertEqual(tiers, {2})
            self.assertEqual(checkpoint["start_tier"], 2)

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

    def test_deferred_event_remains_eligible_for_next_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            settings = Settings(test_root, deepcopy(self.settings.raw))
            discovery = LiveDiscovery(settings)
            prior = discovery.wf.init_run("live")
            event = EventItem(
                id="deferred", source_id="s", source_name="s", source_level=1,
                title="北京市某项公共服务执行信息", url="https://example.test/deferred",
                published_at="2026-07-13T00:00:00+00:00", summary="内容", region="北京",
            )
            discovery._persist_events(prior, [event])
            discovery._record_efficiency(
                prior, premodel_count=1, repeated_excluded=0, model_input_count=0,
                cache_hit=False, tokens_saved=0, deferred_count=1,
                expansion_tier=1, candidate_count=0,
            )
            current = discovery.wf.init_run("live")
            fresh, excluded = discovery._exclude_unchanged([event], current, "live")
            self.assertEqual([item.id for item in fresh], ["deferred"])
            self.assertEqual(excluded, 0)

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
