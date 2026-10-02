from copy import deepcopy
from datetime import datetime, timezone

from sqmy.models import EventItem
from sqmy.screener import deduplicate


def test_distinct_numbered_official_reports_are_not_title_duplicates():
    first = EventItem('environment', 'index', '审计署', 1,
        '审计署审计结果公告2026年第8号：生态环境专项审计结果',
        'https://audit.gov.cn/report/8', datetime.now(timezone.utc).isoformat(), '', '全国',
        material={'discovery_provenance': {'material_kind': 'audit_report'}})
    other = deepcopy(first)
    other.id, other.url = 'water', 'https://audit.gov.cn/report/9'
    other.title = '审计署审计结果公告2026年第9号：水资源专项审计结果'
    assert len(deduplicate([first, other], .80)) == 2
    reprint = deepcopy(first)
    reprint.id, reprint.url = 'reprint', 'https://audit.gov.cn/reprint/8'
    assert len(deduplicate([first, reprint], .80)) == 1
