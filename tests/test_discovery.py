from datetime import datetime, timedelta, timezone
from copy import deepcopy
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from sqmy.collector import SourceCollector, canonical_url, infer_event_region
from sqmy.budget import BudgetExceeded
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery, same_origin_event
from sqmy.models import EventItem
from sqmy.providers import ModelResult, ProviderError
from sqmy.screener import classify


class FakeRouter:
    def __init__(self, data, input_tokens=100, output_tokens=20):
        self.data = data
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls = 0
        self.prompt = None
        self.schema = None

    def analyze(self, prompt, schema):
        self.calls += 1
        self.prompt = prompt
        self.schema = schema
        return ModelResult(
            self.data,
            self.input_tokens,
            self.output_tokens,
            "fake",
            "codex_cli",
            "test",
        ), None


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

    def test_national_livelihood_topic_scope_is_classified(self):
        cases = [
            ("物业公司收费不透明导致业主维权难", "消费者权益与市场秩序"),
            ("农村养老服务补贴申请门槛和多头证明问题", "三农与乡村公共服务"),
            ("无障碍设施被占用导致轮椅使用者出行困难", "残障人与无障碍权益"),
        ]
        for index, (title, expected) in enumerate(cases):
            event = EventItem(
                id=str(index), source_id="s", source_name="s", source_level=2,
                title=title, url=f"https://example.test/{index}",
                published_at=datetime.now(timezone.utc).isoformat(), summary="真实民生痛点",
                region="全国",
            )
            self.assertIn(expected, classify(event))

    def test_source_expansion_tier_is_carried_to_event(self):
        payload = [{
            "source": {"id": "national", "name": "全国来源", "level": 1, "region": "全国", "expansion_tier": 3},
            "xml": "<rss><channel><item><title>全国青年就业政策问题</title><link>https://example.gov.cn/youth</link><description>公开调查数据</description><pubDate>Mon, 13 Jul 2026 00:00:00 GMT</pubDate></item></channel></rss>",
        }]
        events = SourceCollector(self.root, self.settings.raw)._items_from_payloads(payload)
        self.assertEqual(events[0].expansion_tier, 3)

    def test_source_catalog_has_unique_ids_and_national_clue_layers(self):
        sources = SourceCollector(self.root, self.settings.raw).sources
        ids = [source["id"] for source in sources]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(
            sum(int(source.get("expansion_tier", 1)) == 3 for source in sources),
            15,
        )
        clue_sources = [
            source for source in sources
            if int(source.get("expansion_tier", 1)) == 4
        ]
        self.assertTrue(clue_sources)
        self.assertTrue(all(int(source["level"]) == 3 for source in clue_sources))

    def test_live_discovery_records_clue_snapshot_and_tier_four_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "mock"
            settings = Settings(test_root, raw)
            fixture = test_root / "fixture.json"
            fixture.write_text("[]", encoding="utf-8")
            clues = test_root / "clues.jsonl"
            clues.write_text(
                json.dumps(
                    {
                        "title": "老年人医保报销反复提交材料和申诉不畅",
                        "url": "https://forum.example.test/post/1",
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "summary": "公开讨论提示办事门槛、多头证明和申诉困难，仅作待核线索。",
                        "source_name": "公开论坛",
                        "source_level": 3,
                        "region": "全国",
                    },
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )

            run_id, candidates = LiveDiscovery(settings).run(
                fixture, clue_file=clues
            )

            self.assertEqual(candidates, [])
            checkpoint = json.loads(
                LiveDiscovery(settings).wf.status(run_id, include_all=True)[0]["checkpoint_json"]
            )
            self.assertEqual(checkpoint["expansion_tier"], 4)
            self.assertEqual(checkpoint["deferred_count"], 1)
            self.assertTrue(Path(checkpoint["clue_file"]).exists())

    def test_counterevidence_search_uses_longer_policy_window(self):
        old_policy_date = format_datetime(datetime.now(timezone.utc) - timedelta(days=180))
        xml = (
            "<rss><channel><item><title>北京市印发中试平台支持政策</title>"
            "<link>https://fgw.beijing.gov.cn/policy/pilot</link>"
            "<description>政策明确智能制造项目支持、考核评估和结果应用。</description>"
            f"<pubDate>{old_policy_date}</pubDate></item></channel></rss>"
        )
        collector = SourceCollector(self.root, self.settings.raw)
        payload = [{
            "source": {
                "id": "counter", "name": "反证", "level": 2,
                "region": "全国", "expansion_tier": 1,
            },
            "xml": xml,
        }]
        self.assertEqual(collector._items_from_payloads(payload), [])
        with patch.object(collector, "_fetch_query", return_value=xml):
            hits = collector.search_query("北京 中试平台 支持政策", lookback_days=730)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source_level, 1)

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
            discovery._should_run_model(events, force=False, screen_now=True),
            (True, "manual_screen_now"),
        )
        old = (
            datetime.now(timezone.utc)
            - timedelta(hours=self.settings.section("discovery")["pending_batch_max_wait_hours"] + 1)
        ).isoformat()
        self.assertEqual(
            discovery._should_run_model(events, force=False, oldest_pending_at=old),
            (True, "pending_age_limit_reached"),
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

    def test_final_candidate_order_uses_configured_score_not_model_order(self):
        discovery = LiveDiscovery(self.settings)
        now = datetime.now(timezone.utc).isoformat()
        low = EventItem(
            id="low", source_id="s", source_name="s", source_level=1,
            title="北京市低分候选", url="https://example.test/low",
            published_at=now, summary="政策执行问题", region="北京",
            topics=["社区治理"], rule_score=99,
        )
        high = EventItem(
            id="high", source_id="s", source_name="s", source_level=1,
            title="北京市高分候选", url="https://example.test/high",
            published_at=now, summary="政策执行问题", region="北京",
            topics=["社区治理"], rule_score=40,
        )
        low.timeliness_score = high.timeliness_score = 20
        low.model_analysis = {
            "suggested_title": "低分候选",
            "score_components": {key: 0 for key in (
                "beijing_relevance", "pain_authenticity", "policy_window",
                "data_verifiability", "operability", "mechanism_innovation",
            )},
            "applied_penalties": [],
        }
        high.model_analysis = {
            "suggested_title": "高分候选",
            "score_components": {
                "beijing_relevance": 0,
                "pain_authenticity": 15,
                "policy_window": 15,
                "data_verifiability": 10,
                "operability": 10,
                "mechanism_innovation": 5,
            },
            "applied_penalties": [],
        }
        candidates = discovery._rank_candidates([low, high], [])
        self.assertEqual(candidates[0].title, "高分候选")
        self.assertGreater(candidates[0].score, candidates[1].score)
        self.assertEqual(candidates[0].id, "C1")

    def test_small_fixture_finishes_with_zero_model_calls(self):
        titles = ["北京市养老服务政策执行观察", "北京市人工智能企业治理机制观察"]
        items = "".join(
            f"<item><title>{title}</title>"
            f"<link>https://example.gov.cn/small/{index}</link>"
            "<description>公开数据反映相关政策执行存在问题。</description>"
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

    def test_final_tier_without_candidate_records_terminal_reason(self):
        topics = ["未成年人教育", "养老服务", "青年就业", "食品安全"]
        items = "".join(
            f"<item><title>全国{topic}公共服务政策执行问题</title>"
            f"<link>https://example.gov.cn/final/{index}</link>"
            f"<description>公开数据反映{topic}公共服务的制度执行问题。</description>"
            f"<pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate></item>"
            for index, topic in enumerate(topics)
        )
        payload = [{
            "source": {
                "id": "tier3", "name": "第三层一级信源", "level": 1,
                "region": "全国", "type": "rss_search", "query": "fixture", "expansion_tier": 3,
            },
            "xml": "<?xml version='1.0'?><rss><channel>" + items + "</channel></rss>",
        }]
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            settings = Settings(test_root, raw)
            fixture = test_root / "fixture.json"
            fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch("sqmy.discovery.build_router", return_value=FakeRouter({"selections": []})):
                run_id, candidates = LiveDiscovery(settings).run(fixture, start_tier=3)
            status = LiveDiscovery(settings).wf.status(run_id, include_all=True)[0]
            checkpoint = json.loads(status["checkpoint_json"])
            self.assertEqual(candidates, [])
            self.assertEqual(status["status"], "skipped")
            self.assertEqual(checkpoint["next"], "none")
            self.assertIn("已完成全部可用扩展层", checkpoint["skip_reason"])
            report = (test_root / "outputs/candidates" / f"{run_id}.md").read_text(encoding="utf-8")
            self.assertIn("未经高等级来源核验的三级线索", report)

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

    def test_discovery_resume_reuses_same_run_id(self):
        topics = ["教育", "养老", "社区", "就业", "住房", "食品安全", "人工智能", "数据治理"]
        items = "".join(
            f"<item><title>北京市{topic}政策执行问题{index}</title>"
            f"<link>https://example.gov.cn/resume/{index}</link>"
            f"<description>公开数据反映{topic}公共服务存在政策执行问题。</description>"
            f"<pubDate>{format_datetime(datetime.now(timezone.utc))}</pubDate></item>"
            for index, topic in enumerate(topics)
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
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "mock"
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            fixture = test_root / "fixture.json"
            fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            run_id = discovery.wf.init_run("test_fixture")
            discovery.wf.db.checkpoint(
                run_id,
                phase="discovery",
                status="paused_budget",
                data={"start_tier": 1, "resume_next": f"sqmy scan --resume {run_id}"},
            )
            discovery.wf.resume(run_id)
            resumed_id, candidates = discovery.run(fixture, resume_run_id=run_id)
            self.assertEqual(resumed_id, run_id)
            self.assertEqual(len(candidates), 5)

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
            changed = EventItem(**{**event.__dict__, "summary": "新增政策信息改变了制度判断"})
            reopened, changed_excluded = discovery._exclude_unchanged(
                [changed], current, "live"
            )
            self.assertEqual([item.id for item in reopened], ["same"])
            self.assertEqual(changed_excluded, 0)

    def test_history_exclusion_precedes_initial_rule_cap_so_fresh_events_backfill(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(
                self.root / "config/policy_mechanisms.toml",
                test_root / "config/policy_mechanisms.toml",
            )
            raw = deepcopy(self.settings.raw)
            raw["discovery"]["title_similarity_threshold"] = 1.0
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            events = [
                EventItem(
                    id=f"event-{index}", source_id="s", source_name="s", source_level=1,
                    title=f"北京市养老服务政策执行问题主题{index:02d}",
                    url=f"https://example.gov.cn/backfill/{index}",
                    published_at=(
                        datetime.now(timezone.utc) - timedelta(days=index)
                    ).isoformat(),
                    summary="公开数据反映养老服务存在制度负担和执行问题。",
                    region="北京",
                )
                for index in range(36)
            ]
            prior = discovery.wf.init_run("live")
            discovery._persist_events(prior, events[:30])
            discovery._record_efficiency(
                prior, premodel_count=30, repeated_excluded=0, model_input_count=30,
                cache_hit=False, tokens_saved=0, deferred_count=0,
                new_event_count=30, reopened_event_count=0, pending_before_count=30,
                expansion_tier=1, candidate_count=0,
            )
            current = discovery.wf.init_run("live")

            result = discovery._prepare_tier_pool(
                events, current, mode="live", force=False
            )

            self.assertEqual(len(result["rule_results"]), 36)
            self.assertEqual(len(result["history_exclusions"]), 30)
            self.assertEqual(
                {item.id for item in result["premodel_pool"]},
                {f"event-{index}" for index in range(30, 36)},
            )

    def test_premodel_pool_reserves_limited_slots_for_low_trust_clues(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["discovery"]["title_similarity_threshold"] = 1.0
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            events = [
                EventItem(
                    id=f"official-{index}", source_id=f"official-{index}",
                    source_name=f"权威来源{index}", source_level=1,
                    title=f"全国医疗服务政策执行问题{index}",
                    url=f"https://official{index}.gov.cn/item",
                    published_at=datetime.now(timezone.utc).isoformat(),
                    summary="公开数据反映医疗服务存在制度负担和执行问题。",
                    region="全国",
                )
                for index in range(20)
            ]
            events += [
                EventItem(
                    id=f"clue-{index}", source_id=f"clue-{index}",
                    source_name=f"投诉线索{index}", source_level=3,
                    title=f"老年人医保报销反复上传材料维权困难{index}",
                    url=f"https://forum.example.test/{index}",
                    published_at=datetime.now(timezone.utc).isoformat(),
                    summary="投诉反映多头证明、申诉不畅和办事门槛问题。",
                    region="全国",
                    expansion_tier=4,
                )
                for index in range(4)
            ]
            current = discovery.wf.init_run("live")

            result = discovery._prepare_tier_pool(
                events, current, mode="live", force=False
            )

            clue_count = sum(
                item.source_level == 3 for item in result["premodel_pool"]
            )
            self.assertEqual(len(result["premodel_pool"]), 12)
            self.assertEqual(clue_count, 2)

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
                new_event_count=1, reopened_event_count=0, pending_before_count=1,
                expansion_tier=1, candidate_count=0,
            )
            current = discovery.wf.init_run("live")
            fresh, excluded = discovery._exclude_unchanged([event], current, "live")
            self.assertEqual([item.id for item in fresh], ["deferred"])
            self.assertEqual(excluded, 0)

    def test_daily_queue_accumulates_small_batch_and_reopens_changed_url(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(
                self.root / "config/policy_mechanisms.toml",
                test_root / "config/policy_mechanisms.toml",
            )
            settings = Settings(test_root, deepcopy(self.settings.raw))
            discovery = LiveDiscovery(settings)
            run_id = discovery.wf.init_run("live")
            events = [
                EventItem(
                    id=f"queued-{index}", source_id="s", source_name="s", source_level=1,
                    title=f"北京市公共服务执行问题{index}",
                    url=f"https://example.test/queue/{index}",
                    published_at=datetime.now(timezone.utc).isoformat(),
                    summary="公开信息反映制度执行问题", region="北京",
                    topics=["社区治理"], rule_score=70 + index,
                )
                for index in range(2)
            ]

            self.assertEqual(discovery._enqueue_events(run_id, events), (2, 0))
            pending, oldest = discovery._pending_events()
            self.assertEqual({item.id for item in pending}, {"queued-0", "queued-1"})
            self.assertEqual(
                discovery._should_run_model(pending, force=False, oldest_pending_at=oldest),
                (False, "deferred_small_batch"),
            )

            discovery._mark_queue_screened(run_id, pending)
            self.assertEqual(discovery._pending_queue_count(), 0)
            changed = events[0]
            changed.summary = "新增政策信息改变了原有制度缺口判断"
            second_run = discovery.wf.init_run("live")
            self.assertEqual(discovery._enqueue_events(second_run, [changed]), (0, 1))
            reopened, _ = discovery._pending_events()
            self.assertEqual([item.id for item in reopened], ["queued-0"])
            with discovery.wf.db.connect() as conn:
                row = conn.execute(
                    "SELECT status,reopen_count FROM discovery_queue WHERE event_key=?",
                    (discovery._event_key(changed),),
                ).fetchone()
            self.assertEqual((row["status"], row["reopen_count"]), ("pending", 1))

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
                settings.raw["budget"]["weekly_token_limit"] = 0
                second_run = discovery.wf.init_run("test_fixture")
                second, second_hit, saved = discovery._model_rank(second_run, [event])
            self.assertEqual([x.id for x in first], ["cache"])
            self.assertEqual([x.id for x in second], ["cache"])
            self.assertFalse(first_hit)
            self.assertTrue(second_hit)
            self.assertEqual(saved, 120)
            self.assertEqual(router.calls, 1)

    def test_replay_reuses_saved_analysis_with_configured_candidate_score(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            settings = Settings(test_root, deepcopy(self.settings.raw))
            discovery = LiveDiscovery(settings)
            source_run = discovery.wf.init_run("live")
            event = EventItem(
                id="replay-event", source_id="s", source_name="s", source_level=1,
                title="北京市养老服务支付政策执行问题",
                url="https://example.gov.cn/replay-event",
                published_at=datetime.now(timezone.utc).isoformat(),
                summary="公开信息反映支付衔接问题。", region="北京",
                topics=["养老与医疗"], rule_score=70,
                collected_at=datetime.now(timezone.utc).isoformat(),
            )
            discovery._persist_events(source_run, [event])
            analysis = {
                "selections": [{
                    "id": "replay-event",
                    "suggested_title": "关于优化北京市养老服务支付衔接机制的建议",
                    "gap_hypothesis": "现行支付衔接的执行效果需要核验",
                    "gap_type": "effectiveness_gap",
                    "counter_queries": ["北京 养老 支付 衔接 政策"],
                    "score_components": {
                        "beijing_relevance": 0,
                        "pain_authenticity": 12,
                        "policy_window": 10,
                        "data_verifiability": 8,
                        "operability": 8,
                        "mechanism_innovation": 4,
                    },
                    "applied_penalties": [],
                }],
            }
            discovery._write_screening_audit(source_run, analysis)
            replay_run, candidates = discovery.replay(source_run)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].score_reasons["评分版本"], "configured_100_v1")
            self.assertEqual(candidates[0].title, analysis["selections"][0]["suggested_title"])
            with discovery.wf.db.connect() as conn:
                call_count = conn.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (replay_run,)
                ).fetchone()[0]
            self.assertEqual(call_count, 0)

    def test_model_can_explicitly_return_no_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            run_id = discovery.wf.init_run("live")
            event = EventItem(
                id="none", source_id="s", source_name="s", source_level=1,
                title="北京市常规会议消息", url="https://example.gov.cn/none",
                published_at=datetime.now(timezone.utc).isoformat(), summary="未发现制度问题", region="北京",
                topics=["社区治理"], rule_score=70,
            )
            router = FakeRouter({"selections": []})
            with patch("sqmy.discovery.build_router", return_value=router):
                result, cache_hit, _ = discovery._model_rank(run_id, [event])
            self.assertEqual(result, [])
            self.assertFalse(cache_hit)
            self.assertIn('"source_name": "s"', router.prompt)
            self.assertEqual(router.schema["properties"]["selections"]["minItems"], 0)
            selection_schema = router.schema["properties"]["selections"]["items"]
            self.assertIn("score_components", selection_schema["required"])
            self.assertEqual(
                selection_schema["properties"]["score_components"]["properties"]["pain_authenticity"]["maximum"],
                settings.raw["scoring"]["pain_authenticity"],
            )
            with discovery.wf.db.connect() as conn:
                task = conn.execute(
                    "SELECT status,error FROM tasks WHERE run_id=? AND kind='model_screening'",
                    (run_id,),
                ).fetchone()
            self.assertEqual(task["status"], "completed")
            self.assertIsNone(task["error"])

    def test_invalid_model_selection_is_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            run_id = discovery.wf.init_run("live")
            event = EventItem(
                id="valid", source_id="s", source_name="s", source_level=1,
                title="北京市劳动用工政策执行问题", url="https://example.gov.cn/invalid",
                published_at=datetime.now(timezone.utc).isoformat(), summary="公开材料反映用工责任问题", region="北京",
                topics=["平台经济与劳动权益"], rule_score=80,
            )
            invalid = {"selections": [{"id": ""}]}
            with patch("sqmy.discovery.build_router", return_value=FakeRouter(invalid)):
                with self.assertRaises(ProviderError):
                    discovery._model_rank(run_id, [event])
            with discovery.wf.db.connect() as conn:
                task = conn.execute(
                    "SELECT status,error,result_json FROM tasks WHERE run_id=? AND kind='model_screening'",
                    (run_id,),
                ).fetchone()
                call = conn.execute(
                    "SELECT status FROM model_calls WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                run = conn.execute(
                    "SELECT status,error FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            self.assertEqual(task["status"], "failed")
            self.assertIn("有效候选ID", task["error"])
            self.assertEqual(json.loads(task["result_json"]), invalid)
            self.assertIn("invalid_result", call["status"])
            self.assertEqual(run["status"], "failed")
            self.assertIn("有效候选ID", run["error"])

    def test_budget_pause_preserves_discovery_resume_context(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            raw["budget"]["weekly_token_limit"] = 0
            raw["budget"]["complete_started_task_on_budget_exhaustion"] = False
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            run_id = discovery.wf.init_run("live")
            discovery.wf.db.checkpoint(
                run_id,
                phase="discovery",
                status="running",
                data={"start_tier": 3, "next": "model_screening"},
            )
            event = EventItem(
                id="budget",
                source_id="s",
                source_name="s",
                source_level=1,
                title="全国教育政策执行问题",
                url="https://example.gov.cn/budget",
                published_at=datetime.now(timezone.utc).isoformat(),
                summary="公开数据反映未成年人公共服务问题",
                region="全国",
                topics=["教育和未成年人"],
                rule_score=70,
            )
            with patch("sqmy.discovery.build_router", return_value=FakeRouter({"selections": [{"id": "budget"}]})):
                with self.assertRaises(BudgetExceeded):
                    discovery._model_rank(run_id, [event])
            checkpoint = json.loads(discovery.wf.status(run_id, include_all=True)[0]["checkpoint_json"])
            self.assertEqual(checkpoint["start_tier"], 3)
            self.assertEqual(checkpoint["resume_next"], f"sqmy scan --resume {run_id}")

    def test_run_returns_cleanly_when_pre_model_budget_blocks_screening(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            raw["budget"]["weekly_token_limit"] = 0
            raw["budget"]["complete_started_task_on_budget_exhaustion"] = False
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)

            with patch("sqmy.discovery.build_router", return_value=FakeRouter({"selections": []})):
                run_id, candidates = discovery.run(
                    self.root / "tests/fixtures/monday_observability.json"
                )

            status = discovery.wf.status(run_id, include_all=True)[0]
            checkpoint = json.loads(status["checkpoint_json"])
            self.assertEqual(candidates, [])
            self.assertEqual(status["phase"], "discovery")
            self.assertEqual(status["status"], "paused_budget")
            self.assertEqual(checkpoint["resume_next"], f"sqmy scan --resume {run_id}")
            with discovery.wf.db.connect() as conn:
                calls = conn.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            self.assertEqual(calls, 0)

    def test_started_scan_finishes_then_records_budget_action(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            raw["budget"]["weekly_token_limit"] = 0
            raw["budget"]["complete_started_task_on_budget_exhaustion"] = True
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)

            with patch("sqmy.discovery.build_router", return_value=FakeRouter({"selections": []})):
                run_id, candidates = discovery.run(
                    self.root / "tests/fixtures/monday_observability.json"
                )

            self.assertEqual(candidates, [])
            status = discovery.wf.status(run_id, include_all=True)[0]
            checkpoint = json.loads(status["checkpoint_json"])
            self.assertEqual(status["status"], "skipped")
            self.assertIn("budget_overrun", checkpoint)
            self.assertIn("提额", checkpoint["budget_action_required"])
            with discovery.wf.db.connect() as conn:
                calls = conn.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            self.assertEqual(calls, 1)

    def test_actual_token_overrun_saves_result_then_pauses_following_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            test_root = Path(temp)
            (test_root / "config").mkdir()
            shutil.copy(self.root / "config/sources.toml", test_root / "config/sources.toml")
            shutil.copy(self.root / "config/policy_mechanisms.toml", test_root / "config/policy_mechanisms.toml")
            raw = deepcopy(self.settings.raw)
            raw["model"]["provider"] = "codex_cli"
            raw["budget"]["weekly_token_limit"] = 200_000
            raw["budget"]["screening_tokens"] = 45_000
            raw["budget"]["complete_started_task_on_budget_exhaustion"] = False
            settings = Settings(test_root, raw)
            discovery = LiveDiscovery(settings)
            run_id = discovery.wf.init_run("live")
            event = EventItem(
                id="overrun",
                source_id="s",
                source_name="s",
                source_level=1,
                title="北京市未成年人公共服务政策执行问题",
                url="https://example.gov.cn/overrun",
                published_at=datetime.now(timezone.utc).isoformat(),
                summary="公开材料反映政策执行和举证机制问题",
                region="北京",
                topics=["教育和未成年人"],
                rule_score=80,
            )
            router = FakeRouter(
                {"selections": [{"id": "overrun"}]},
                input_tokens=50_000,
                output_tokens=1_000,
            )
            with patch("sqmy.discovery.build_router", return_value=router):
                result, cache_hit, _ = discovery._model_rank(run_id, [event])
            self.assertEqual([item.id for item in result], ["overrun"])
            self.assertFalse(cache_hit)
            with discovery.wf.db.connect() as conn:
                call = conn.execute(
                    "SELECT estimated_tokens,stage_limit,over_budget FROM model_calls WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                task = conn.execute(
                    "SELECT status,result_json FROM tasks WHERE run_id=? AND kind='model_screening'",
                    (run_id,),
                ).fetchone()
            self.assertEqual(call["stage_limit"], 45_000)
            self.assertEqual(call["over_budget"], 1)
            self.assertEqual(task["status"], "completed")
            self.assertIsNotNone(task["result_json"])
            status = discovery.wf.status(run_id, include_all=True)[0]
            self.assertEqual(status["status"], "paused_budget")
