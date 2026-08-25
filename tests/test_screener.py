from datetime import datetime, timedelta, timezone

from sqmy.models import EventItem
from sqmy.screener import (
    configured_candidate_score,
    local_date,
    rule_screen,
    rule_screen_with_decisions,
    timeliness_score,
    to_candidate,
)


SCORING = {
    "haidian_relevance": 15,
    "beijing_relevance": 10,
    "timeliness": 20,
    "pain_authenticity": 15,
    "policy_window": 15,
    "data_verifiability": 10,
    "operability": 10,
    "mechanism_innovation": 5,
}
PENALTIES = {
    "policy_fully_covered": 15,
    "weak_sources": 15,
    "no_local_landing": 15,
    "no_executable_actor": 15,
}


def test_timeliness_score_decays_with_age():
    now = datetime.now(timezone.utc)
    assert timeliness_score((now - timedelta(days=2)).isoformat(), now) == 20
    assert timeliness_score((now - timedelta(days=20)).isoformat(), now) == 15
    assert timeliness_score((now - timedelta(days=75)).isoformat(), now) == 3


def test_local_date_uses_beijing_calendar_day():
    assert local_date("2026-08-12T17:00:00+00:00") == "2026-08-13"


def test_model_suggested_title_replaces_truncated_news_title():
    item = EventItem(
        id="x", source_id="s", source_name="s", source_level=2,
        title="涉未成年人网络纠纷5年增长近20倍 北京互联网法院发布《未成年人...",
        url="https://example.test", published_at=datetime.now(timezone.utc).isoformat(),
        summary="摘要", region="北京", topics=["教育和未成年人"], rule_score=70,
    )
    item.model_analysis = {"suggested_title": "关于完善北京市未成年人网络纠纷前端治理机制的建议"}
    candidate = to_candidate(item, 1, SCORING, PENALTIES)
    assert candidate.title == item.model_analysis["suggested_title"]


def test_candidate_score_uses_configured_dimensions_and_penalties():
    item = EventItem(
        id="score", source_id="s", source_name="一级来源", source_level=1,
        title="海淀区公共服务政策执行问题", url="https://example.test/score",
        published_at=datetime.now(timezone.utc).isoformat(), summary="摘要", region="海淀",
        topics=["社区治理"], rule_score=99,
    )
    item.timeliness_score = 20
    item.model_analysis = {
        "score_components": {
            "beijing_relevance": 10,
            "pain_authenticity": 12,
            "policy_window": 10,
            "data_verifiability": 8,
            "operability": 7,
            "mechanism_innovation": 4,
        },
        "applied_penalties": [
            {"key": "no_executable_actor", "reason": "执行主体仍需核准"},
        ],
    }
    result, reasons = configured_candidate_score(
        item, SCORING, PENALTIES, novelty_penalty=12
    )
    # 海淀15 + 北京0（不重复） + 时效20 + 12 + 10 + 8 + 7 + 4 - 15 - 12
    assert result == 49
    assert reasons["正向分项"]["北京相关性"]["得分"] == 0
    assert "避免重复" in reasons["正向分项"]["北京相关性"]["理由"]
    assert reasons["扣分合计"] == 27
    assert reasons["最终得分"] == 49
    assert item.rule_score == 99


def test_national_topic_is_not_penalized_only_for_lacking_beijing_landing():
    item = EventItem(
        id="national", source_id="s", source_name="一级来源", source_level=1,
        title="全国养老政策问题", url="https://example.test/national",
        published_at=datetime.now(timezone.utc).isoformat(), summary="摘要", region="全国",
    )
    item.timeliness_score = 20
    item.model_analysis = {
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
    result, reasons = configured_candidate_score(item, SCORING, PENALTIES)
    assert result == 75
    assert reasons["扣分项"] == []


def test_low_trust_complaint_language_can_reach_rule_screen_as_a_clue():
    item = EventItem(
        id="clue", source_id="complaint", source_name="投诉线索", source_level=3,
        title="老年人线上办理医保报销遇到反复提交材料和申诉无门",
        url="https://example.test/clue",
        published_at=datetime.now(timezone.utc).isoformat(),
        summary="用户反映办事门槛高、多头证明、处理不畅。",
        region="全国",
    )

    selected = rule_screen(
        [item], {"title_similarity_threshold": 0.88, "initial_max": 30}
    )

    assert [event.id for event in selected] == ["clue"]
    assert "养老与医疗" in item.topics


def test_scoring_rejects_weights_that_do_not_total_100():
    item = EventItem(
        id="bad", source_id="s", source_name="s", source_level=1,
        title="政策问题", url="https://example.test/bad",
        published_at=datetime.now(timezone.utc).isoformat(), summary="摘要", region="北京",
    )
    invalid = SCORING | {"timeliness": 19}
    try:
        configured_candidate_score(item, invalid, PENALTIES)
    except ValueError as exc:
        assert "必须为100" in str(exc)
    else:
        raise AssertionError("无效评分权重应被拒绝")


def test_rule_screen_decisions_preserve_results_and_explain_exclusions():
    now = datetime.now(timezone.utc).isoformat()
    qualified = EventItem(
        id="qualified", source_id="s", source_name="s", source_level=1,
        title="北京市养老服务政策执行问题", url="https://example.test/qualified",
        published_at=now, summary="公开数据反映相关群体负担", region="北京",
    )
    no_topic = EventItem(
        id="no-topic", source_id="s", source_name="s", source_level=1,
        title="北京市某项常规信息", url="https://example.test/no-topic",
        published_at=now, summary="公开材料反映问题", region="北京",
    )
    activity = EventItem(
        id="activity", source_id="s", source_name="s", source_level=1,
        title="北京市养老服务会议召开", url="https://example.test/activity",
        published_at=now, summary="政策活动信息", region="北京",
    )
    duplicate = EventItem(
        id="duplicate", source_id="s2", source_name="s2", source_level=2,
        title=qualified.title, url="https://mirror.test/qualified",
        published_at=now, summary=qualified.summary, region="北京",
    )
    settings = {"title_similarity_threshold": 0.88, "initial_max": 30}

    original = rule_screen([qualified, no_topic, activity, duplicate], settings)
    selected, exclusions = rule_screen_with_decisions(
        [qualified, no_topic, activity, duplicate], settings
    )

    assert [item.id for item in selected] == [item.id for item in original]
    reasons = {item["event"].id: item["reason_code"] for item in exclusions}
    assert reasons["no-topic"] == "no_topic"
    assert reasons["activity"] == "empty_phrase"
    assert reasons["duplicate"] in {"duplicate_title", "duplicate_url"}
