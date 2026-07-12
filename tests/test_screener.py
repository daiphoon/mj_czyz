from datetime import datetime, timedelta, timezone

from sqmy.models import EventItem
from sqmy.screener import timeliness_score, to_candidate


def test_timeliness_score_decays_with_age():
    now = datetime.now(timezone.utc)
    assert timeliness_score((now - timedelta(days=2)).isoformat(), now) == 20
    assert timeliness_score((now - timedelta(days=20)).isoformat(), now) == 15
    assert timeliness_score((now - timedelta(days=75)).isoformat(), now) == 3


def test_model_suggested_title_replaces_truncated_news_title():
    item = EventItem(
        id="x", source_id="s", source_name="s", source_level=2,
        title="涉未成年人网络纠纷5年增长近20倍 北京互联网法院发布《未成年人...",
        url="https://example.test", published_at=datetime.now(timezone.utc).isoformat(),
        summary="摘要", region="北京", topics=["教育和未成年人"], rule_score=70,
    )
    item.model_analysis = {"suggested_title": "关于完善北京市未成年人网络纠纷前端治理机制的建议"}
    candidate = to_candidate(item, 1)
    assert candidate.title == item.model_analysis["suggested_title"]
