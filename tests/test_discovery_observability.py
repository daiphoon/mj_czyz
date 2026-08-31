from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import pytest

from sqmy.collector import SourceCollector
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery
from sqmy.discovery_shadow import ShadowVerifier, write_discovery_evaluation
from sqmy.db import now
from sqmy.models import EventItem


ROOT = Path(__file__).parents[1]
BASE_SETTINGS = Settings.load(ROOT / "config/settings.toml")


def _settings(root: Path, *, shadow_enabled: bool = True) -> Settings:
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "config/sources.toml", root / "config/sources.toml")
    shutil.copy(ROOT / "config/policy_mechanisms.toml", root / "config/policy_mechanisms.toml")
    raw = deepcopy(BASE_SETTINGS.raw)
    raw["model"]["provider"] = "mock"
    raw["shadow_verification"]["enabled"] = shadow_enabled
    return Settings(root, raw)


def _fixture_payload() -> list[dict]:
    topics = ["养老服务", "人工智能", "青年就业", "食品安全", "社区治理", "中小企业", "住房租赁", "交通停车"]
    current = format_datetime(datetime.now(timezone.utc))
    items = "".join(
        f"<item><title>北京市{topic}政策执行问题{index}</title>"
        f"<link>https://example.gov.cn/item/{index}</link>"
        f"<description>公开数据反映{topic}存在制度执行问题和群体负担。</description>"
        f"<pubDate>{current}</pubDate></item>"
        for index, topic in enumerate(topics)
    )
    items += (
        "<item><title>北京市某项常规信息</title>"
        "<link>https://example.gov.cn/item/no-topic</link>"
        "<description>公开材料反映问题。</description>"
        f"<pubDate>{current}</pubDate></item>"
        "<item><title>北京市养老服务政策执行问题0</title>"
        "<link>https://mirror.example.test/item/0</link>"
        "<description>公开数据反映养老服务存在制度执行问题和群体负担。</description>"
        f"<pubDate>{current}</pubDate></item>"
    )
    return [{
        "source": {
            "id": "fixture", "name": "离线一级信源", "level": 1,
            "region": "北京", "type": "rss_search", "query": "fixture",
            "expansion_tier": 1,
        },
        "xml": "<?xml version='1.0'?><rss><channel>" + items + "</channel></rss>",
    }]


def test_collector_records_source_stage_counts_for_fixture():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        current = format_datetime(datetime.now(timezone.utc))
        old = format_datetime(datetime.now(timezone.utc) - timedelta(days=120))
        payload = [{
            "source": {
                "id": "fixture", "name": "离线信源", "level": 1,
                "region": "北京", "expansion_tier": 1,
            },
            "xml": (
                "<rss><channel>"
                "<item><title>北京市养老政策问题</title><link>https://example.gov.cn/valid</link>"
                f"<pubDate>{current}</pubDate></item>"
                "<item><title></title><link>https://example.gov.cn/missing</link>"
                f"<pubDate>{current}</pubDate></item>"
                "<item><title>旧政策</title><link>https://example.gov.cn/old</link>"
                f"<pubDate>{old}</pubDate></item>"
                "</channel></rss>"
            ),
        }]
        fixture = root / "fixture.json"
        fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        collector = SourceCollector(root, settings.raw)
        events = collector.collect("run-fixture", fixture=fixture)

        assert len(events) == 1
        stats = collector.collection_stats[0]
        assert stats["fetched_count"] == 3
        assert stats["within_window_count"] == 2
        assert stats["collected_count"] == 1
        assert stats["invalid_metadata_count"] == 1
        assert stats["outside_window_count"] == 1
        audit = json.loads(
            (root / "data/runs/run-fixture/collection_audit.json").read_text(encoding="utf-8")
        )
        assert audit["sources"][0]["source_id"] == "fixture"


def test_collector_imports_auditable_metadata_only_social_clues():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        clue_file = root / "clues.jsonl"
        clue_file.write_text(
            json.dumps(
                {
                    "title": "老年人线上医保报销反复提交材料",
                    "url": "https://tousu.example.test/complaint/1",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "summary": "公开投诉提示申报门槛和申诉不畅，只作为待核线索。",
                    "source_name": "公开投诉平台",
                    "source_level": 3,
                    "region": "全国",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        fixture = root / "fixture.json"
        fixture.write_text("[]", encoding="utf-8")

        collector = SourceCollector(root, settings.raw)
        events = collector.collect("run-clues", fixture=fixture, clue_file=clue_file)

        assert len(events) == 1
        assert events[0].source_level == 3
        assert events[0].expansion_tier == 4
        assert events[0].source_name == "公开投诉平台"
        snapshot = root / "data/runs/run-clues/discovery_clues.jsonl"
        assert snapshot.exists()
        assert "只作为待核线索" in snapshot.read_text(encoding="utf-8")


def test_clue_import_rejects_sensitive_query_parameters():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        clue_file = root / "clues.jsonl"
        clue_file.write_text(
            json.dumps(
                {
                    "title": "公共服务申诉线索",
                    "url": "https://example.test/post?access_token=secret",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "summary": "线索",
                    "source_name": "论坛",
                    "source_level": 3,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        fixture = root / "fixture.json"
        fixture.write_text("[]", encoding="utf-8")

        collector = SourceCollector(root, settings.raw)
        with pytest.raises(ValueError, match="敏感查询参数"):
            collector.collect("run-sensitive", fixture=fixture, clue_file=clue_file)


def test_shadow_review_is_zero_model_and_does_not_change_candidate_output():
    results = []
    for enabled in (True, False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = _settings(root, shadow_enabled=enabled)
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps(_fixture_payload(), ensure_ascii=False), encoding="utf-8")
            discovery = LiveDiscovery(settings)
            run_id, candidates = discovery.run(fixture)
            results.append([
                (item.title, item.score, item.score_reasons["来源URL"])
                for item in candidates
            ])
            with discovery.wf.db.connect() as conn:
                model_calls = conn.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                funnel = conn.execute(
                    "SELECT * FROM source_funnel WHERE run_id=?", (run_id,)
                ).fetchall()
                shadows = conn.execute(
                    "SELECT * FROM discovery_shadow_reviews WHERE run_id=?", (run_id,)
                ).fetchall()
                samples = conn.execute(
                    """SELECT stage,COUNT(*) AS count FROM discovery_exclusion_samples
                       WHERE run_id=? GROUP BY stage""",
                    (run_id,),
                ).fetchall()
            checkpoint = json.loads(discovery.wf.status(run_id, include_all=True)[0]["checkpoint_json"])
            assert model_calls == 0
            assert len(funnel) == 1
            assert funnel[0]["raw_item_count"] == 10
            assert funnel[0]["collected_count"] == 10
            assert funnel[0]["candidate_count"] == 5
            assert len(shadows) == (8 if enabled else 0)
            assert checkpoint["shadow_enforced"] is False
            assert Path(checkpoint["discovery_observability"]).exists()
            assert all(
                row["count"] <= settings.raw["observability"]["exclusion_sample_per_stage"]
                for row in samples
            )
    assert results[0] == results[1]


def test_shadow_review_records_direct_primary_and_known_policy_match():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        discovery = LiveDiscovery(settings)
        run_id = discovery.wf.init_run("test_fixture")
        event = EventItem(
            id="minor", source_id="official", source_name="北京市政府", source_level=1,
            title="北京市未成年人网络平台投诉举报保护机制问题",
            url="https://www.beijing.gov.cn/example/minors",
            published_at=datetime.now(timezone.utc).isoformat(),
            summary="公开信息反映网络平台未成年人投诉和合规问题。",
            region="北京", region_evidence="trusted_domain:beijing.gov.cn",
            topics=["教育和未成年人"], rule_score=80,
        )

        reviews = ShadowVerifier(settings).review(run_id, [event], live_search=False)

        assert reviews[0].original_source_status == "direct_primary"
        assert reviews[0].local_landing_status == "supported"
        assert reviews[0].coverage_status == "known_mechanism_match"
        with discovery.wf.db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
            ).fetchone()[0] == 0


def test_shadow_review_accepts_national_scope_without_beijing_landing():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        discovery = LiveDiscovery(settings)
        run_id = discovery.wf.init_run("test_fixture")
        event = EventItem(
            id="national", source_id="official", source_name="国家部委", source_level=1,
            title="全国医保异地结算执行衔接问题",
            url="https://www.gov.cn/example/national",
            published_at=datetime.now(timezone.utc).isoformat(),
            summary="公开数据反映跨省公共服务存在制度衔接问题。",
            region="全国", region_evidence="source_channel_only:全国",
            topics=["社会保障与社会救助"], rule_score=80,
        )

        review = ShadowVerifier(settings).review(
            run_id, [event], live_search=False
        )[0]

        assert review.local_landing_status == "national_scope"
        assert "local_landing_unverified" not in review.reason_codes


def test_shadow_review_reuses_unchanged_same_run_input_without_new_search():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        discovery = LiveDiscovery(settings)
        run_id = discovery.wf.init_run("live")
        event = EventItem(
            id="cached-shadow", source_id="media", source_name="媒体", source_level=2,
            title="北京市住房租赁流程纠纷问题",
            url="https://media.example.test/housing",
            published_at=datetime.now(timezone.utc).isoformat(),
            summary="公开调查反映租赁流程存在信息不对称。",
            region="北京", region_evidence="text:北京市",
            topics=["住房与公共服务"], rule_score=80,
        )
        verifier = ShadowVerifier(settings)
        with patch.object(verifier, "_search", return_value=([], None)) as search:
            verifier.review(run_id, [event], live_search=True)
            verifier.review(run_id, [event], live_search=True)
        assert search.call_count == 2


def test_rolling_shadow_evaluation_becomes_comparable_after_configured_live_runs():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        discovery = LiveDiscovery(settings)
        run_ids = []
        for index in range(settings.raw["observability"]["minimum_live_runs_for_comparison"]):
            run_id = discovery.wf.init_run("live")
            run_ids.append(run_id)
            stamp = now()
            with discovery.wf.db.connect() as conn:
                conn.execute(
                    """INSERT INTO source_funnel(
                         run_id,source_id,source_name,expansion_tier,included_in_scan,
                         raw_item_count,collected_count,rule_qualified_count,
                         model_input_count,model_selected_count,candidate_count,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, "source", "来源", 1, 1, 10, 8, 6, 5, 3, 1, stamp),
                )
                conn.execute(
                    """INSERT INTO discovery_shadow_reviews(
                         id,run_id,event_id,source_id,input_hash,title,url,event_role,
                         original_source_status,local_landing_status,coverage_status,
                         recommendation,reason_codes_json,policy_matches_json,
                         search_hits_json,model_selected,candidate_selected,enforced,
                         created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{run_id}:event", run_id, f"event-{index}", "source", "hash",
                        "北京市公共服务问题", "https://example.gov.cn/event",
                        "problem_signal", "direct_primary", "supported", "unclear",
                        "eligible_for_comparison", "[]", "[]", "[]", 1, 1, 0,
                        stamp, stamp,
                    ),
                )

        same_day = json.loads(
            write_discovery_evaluation(settings).read_text(encoding="utf-8")
        )
        assert same_day["status"] == "insufficient_observation"
        assert same_day["observation_span_days"] == 0

        for index, run_id in enumerate(run_ids):
            stamp = (
                datetime.now(timezone.utc)
                - timedelta(days=7 * (len(run_ids) - index - 1))
            ).isoformat()
            with discovery.wf.db.connect() as conn:
                conn.execute(
                    "UPDATE source_funnel SET updated_at=? WHERE run_id=?",
                    (stamp, run_id),
                )
                conn.execute(
                    "UPDATE discovery_shadow_reviews SET updated_at=? WHERE run_id=?",
                    (stamp, run_id),
                )
        report = json.loads(
            write_discovery_evaluation(settings).read_text(encoding="utf-8")
        )

        assert report["status"] == "ready_for_comparison"
        assert report["observation_span_days"] >= 14
        assert report["source_funnel"][0]["included_runs"] == 3
        assert report["source_funnel"][0]["considered_collected"] == 24
        assert report["shadow_comparison"][0]["candidates"] == 3


def test_rolling_report_flags_source_health_and_uses_decisive_shadow_followup_only():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        settings = _settings(root)
        discovery = LiveDiscovery(settings)
        reasons = ("policy_covered", "policy_covered", "original_gap_supported")
        coverages = ("known_mechanism_match", "unclear", "possible_coverage")
        recommendations = ("reframe_or_monitor", "eligible_for_comparison", "reframe_or_monitor")
        for index in range(3):
            run_id = discovery.wf.init_run("live")
            stamp = (
                datetime.now(timezone.utc) - timedelta(days=14 - index * 7)
            ).isoformat()
            event_id = f"event-{index}"
            with discovery.wf.db.connect() as conn:
                for source_id, source_name, collected, qualified in (
                    ("zero", "连续零结果来源", 0, 0),
                    ("productive", "有效来源", 8, 6),
                ):
                    conn.execute(
                        """INSERT INTO source_funnel(
                             run_id,source_id,source_name,expansion_tier,included_in_scan,
                             raw_item_count,collected_count,rule_qualified_count,
                             model_input_count,model_selected_count,candidate_count,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id, source_id, source_name, 1, 1, collected,
                            collected, qualified, 5 if collected else 0,
                            3 if collected else 0, 1 if collected else 0, stamp,
                        ),
                    )
                conn.execute(
                    """INSERT INTO event_items(
                         id,run_id,source_id,source_name,source_level,title,url,
                         topics_json,rule_score,content_hash,collected_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{run_id}:{event_id}", run_id, "productive", "有效来源", 1,
                        f"公共服务制度问题{index}", f"https://example.gov.cn/{index}",
                        "[]", 80, f"hash-{index}", stamp,
                    ),
                )
                conn.execute(
                    """INSERT INTO candidates(id,run_id,title,data_json,score,selected,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        f"{run_id}:C1", run_id, f"候选{index}",
                        json.dumps({"id": "C1", "score_reasons": {"事件ID": event_id}}, ensure_ascii=False),
                        80, 1, stamp,
                    ),
                )
                conn.execute(
                    """INSERT INTO discovery_shadow_reviews(
                         id,run_id,event_id,source_id,input_hash,title,url,event_role,
                         original_source_status,local_landing_status,coverage_status,
                         recommendation,reason_codes_json,policy_matches_json,
                         search_hits_json,model_selected,candidate_selected,enforced,
                         created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{run_id}:{event_id}", run_id, event_id, "productive", "hash",
                        f"公共服务制度问题{index}", f"https://example.gov.cn/{index}",
                        "problem_signal", "direct_primary", "national_scope",
                        coverages[index], recommendations[index], "[]", "[]", "[]",
                        1, 1, 0, stamp, stamp,
                    ),
                )
                proceed = index == 2
                conn.execute(
                    """INSERT INTO research_reviews(
                         id,run_id,candidate_id,topic_id,input_hash,decision,confidence,
                         research_allowed,data_json,report_path,human_decision,reviewed_at,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"review-{index}", run_id, "C1", f"topic-{index}", "input",
                        "proceed" if proceed else "stop", "high", int(proceed),
                        json.dumps({"decision_reason": reasons[index]}), "report.md",
                        "proceed" if proceed else "stop", stamp, stamp,
                    ),
                )
                if proceed:
                    conn.execute(
                        """INSERT INTO topics(
                             id,title,created_at,run_id,candidate_id,draft_source_path,
                             review_path,approval_status,actually_submitted
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            "topic-2", "正式稿", stamp, run_id, "C1", "draft.md",
                            "review.docx", "approved", 1,
                        ),
                    )

        report = json.loads(
            write_discovery_evaluation(settings).read_text(encoding="utf-8")
        )

        assert report["status"] == "ready_for_comparison"
        assert report["source_health_policy"]["automatic_source_changes"] is False
        by_source = {item["source_id"]: item for item in report["source_funnel"]}
        assert by_source["zero"]["health_status"] == "degraded"
        assert by_source["zero"]["latest_zero_result_streak"] == 3
        assert by_source["zero"]["recommended_action"] == "review_fetch_or_query_do_not_disable"
        assert by_source["productive"]["health_status"] == "active"
        assert by_source["productive"]["pre_research_proceed"] == 1
        assert by_source["productive"]["drafts"] == 1
        evaluation = report["policy_coverage_shadow_evaluation"]
        assert evaluation["enforced"] is False
        assert evaluation["status"] == "ready_for_assessment"
        assert evaluation["confirmed_coverage_warning_count"] == 1
        assert evaluation["missed_policy_coverage_count"] == 1
        assert evaluation["false_coverage_warning_count"] == 1
        assert evaluation["observed_warning_precision"] == 0.5
        assert evaluation["observed_coverage_recall"] == 0.5
        assert evaluation["automatic_conclusion"] == "improve_policy_coverage_recall"
