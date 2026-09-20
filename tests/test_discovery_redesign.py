from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import patch
import json
import shutil
import xml.etree.ElementTree as ET

import pytest

from sqmy.collector import SourceCollector, listing_to_rss
from sqmy.config import Settings
from sqmy.retrieval import discovery_queries


@pytest.fixture
def collector(tmp_path):
    project = Path(__file__).parents[1]
    shutil.copytree(project / "config", tmp_path / "config")
    raw = deepcopy(Settings.load(project / "config/settings.toml").raw)
    raw['discovery']['rss_endpoint_circuit_breaker'] = True
    return SourceCollector(tmp_path, raw)


def test_shared_html_failure_stops_only_search_endpoint(collector):
    collector.sources = [dict(id=f'q{i}', name=f'q{i}', query=f'q{i}', type='rss_search',
                              region='全国', level=2) for i in range(4)]
    collector.sources.append(dict(id='direct', name='直连', type='html_index', region='北京',
                                  level=1, url='https://example.gov.cn/',
                                  item_url_prefix='https://example.gov.cn/', max_items=2))
    calls = []

    def fetch(url, namespace):
        calls.append(namespace)
        if namespace == 'feeds':
            return '<html><body>搜索首页</body></html>'
        return '<li><a href="a.html">公开答复</a><span>2026-09-20</span></li>'

    with patch.object(collector, '_fetch_url', side_effect=fetch):
        events = collector.collect('fixture-circuit')
        with pytest.raises(ValueError, match='入口'):
            collector.search_query('反证')
    assert calls.count('feeds') == 1
    assert 'counterevidence' not in calls
    assert [e.source_id for e in events] == ['direct']
    skipped = [s for s in collector.collection_stats if s.get('request_status') == 'skipped_endpoint_unavailable']
    assert len(skipped) == 3
    assert all(s['fetch_error'] for s in skipped)


def test_empty_rss_and_query_errors_do_not_trip_shared_breaker(collector):
    collector.sources = [dict(id=f'q{i}', name=f'q{i}', query=f'q{i}', type='rss_search',
                              region='全国', level=2) for i in range(3)]
    def fetch(source):
        if source['id'] == 'q0':
            raise TimeoutError('单个查询超时')
        return '<rss><channel/></rss>'
    with patch.object(collector, '_fetch', side_effect=fetch) as f:
        collector.collect('fixture-local-error')
    assert f.call_count == 3
    assert not any(s.get('request_status') == 'skipped_endpoint_unavailable' for s in collector.collection_stats)


def test_court_date_tag_not_body_or_url_date():
    source = dict(url='https://www.court.gov.cn/fabu.html',
                  item_url_prefix='https://www.court.gov.cn/', max_items=15,
                  listing_format='court_date')
    html = '<li><a href="/fabu/xiangqing/123.html">2025年问题</a><i class="date">2026-09-17</i></li>'
    assert ET.fromstring(listing_to_rss(html, source)).findtext('item/pubDate') == '2026-09-17'
    with pytest.raises(ValueError):
        listing_to_rss(html.replace('class="date"', 'class="other"'), source)


def test_query_roles_share_existing_limit_and_prioritize_missing(collector):
    cfg = {'max_searches_per_action': 4, 'discovery_roles': ['local', 'national', 'scene', 'exploration'],
           'scenario_queries': [dict(id=str(i), query=str(i), signals=[str(i)], role=role)
                                for i, role in enumerate(['local'] * 4 + ['national', 'scene', 'exploration'])]}
    queries = discovery_queries([], cfg, date(2026, 9, 20))
    assert len(queries) == 4
    assert {q['role'] for q in queries} == set(cfg['discovery_roles'])
    legacy = dict(cfg); legacy.pop('discovery_roles')
    assert len(discovery_queries([], legacy, date(2026, 9, 20))) == 4


def test_direct_material_preserves_channel_publisher_and_reply_date(collector):
    source = dict(id='reply', name='答复目录', type='html_index', region='北京', level=1,
                  expansion_tier=2, url='https://www.beijing.gov.cn/hudong/',
                  item_url_prefix='https://www.beijing.gov.cn/hudong/hdjl/', max_items=5,
                  material_kind='public_reply', date_basis='reply_date')
    payload = '<li><a href="/hudong/hdjl/a">办事答复</a><span>2026-09-20</span></li>'
    event = collector._items_from_payloads([{'source': source, 'xml': payload}])[0]
    info = event.material['discovery_provenance']
    assert info['channel'] == 'direct_index'
    assert info['publisher_domain'] == 'www.beijing.gov.cn'
    assert info['date_basis'] == 'reply_date'
    assert info['material_kind'] == 'public_reply'
    assert info['original_chain_status'] == 'not_verified'


def test_coverage_does_not_call_all_tier_four_complaints(collector):
    from sqmy.discovery_coverage import coverage_summary
    from sqmy.models import EventItem
    event = EventItem('a', 's', '政府原文', 1, '标题', 'https://www.gov.cn/a', '', '', '全国',
                      expansion_tier=4, material={'discovery_provenance': {
                          'channel': 'tavily', 'publisher_domain': 'www.gov.cn',
                          'material_kind': 'unclassified', 'date_basis': 'provider_date'}})
    summary = coverage_summary([event, event], [])
    assert summary['unique_urls'] == 1
    assert summary['by_channel'] == {'tavily': 1}
    assert summary['by_material_kind'] == {'unclassified': 1}
    assert summary['by_source_level'] == {'1': 1}


def test_new_collection_rechecks_previously_failed_endpoint(collector):
    collector.sources = [dict(id='q', name='q', query='q', type='rss_search', region='全国', level=2)]
    with patch.object(collector, '_fetch_url', return_value='<html>错误入口</html>'):
        collector.collect('first')
    assert collector._rss_endpoint_error
    with patch.object(collector, '_fetch_url', return_value='<rss><channel/></rss>') as fetch:
        collector.collect('second')
    assert fetch.call_count == 1
    assert collector._rss_endpoint_error is None
    assert not collector.collection_stats[0]['fetch_error']


def test_coverage_without_paid_search_and_legacy_resume(collector):
    from sqmy.retrieval import collect_discovery
    settings = Settings(collector.root, deepcopy(Settings.load().raw))
    settings.raw['tavily']['enabled'] = False
    collector.sources = []
    collect_discovery(collector, settings, None, 'direct-only')
    assert json.loads((collector.root / 'data/runs/direct-only/discovery_coverage.json').read_text())['unique_urls'] == 0
    settings.raw['tavily']['enabled'] = True
    settings.raw['model']['provider'] = 'codex'
    directory = collector.root / 'data/runs/legacy'
    directory.mkdir()
    (directory / 'collection_checkpoint.json').write_text(json.dumps({'finished': True, 'stats': [], 'events': []}))
    with patch.object(collector, 'collect', side_effect=AssertionError('不能重新获取已完成输入')):
        assert collect_discovery(collector, settings, None, 'legacy') == []
    assert (directory / 'discovery_coverage.json').exists()
