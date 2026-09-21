from copy import deepcopy
from datetime import datetime, timezone
import json
import shutil

import pytest

from sqmy.collector import SourceCollector
from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery
from sqmy.models import EventItem
from sqmy.screener import deduplicate, rule_screen_with_decisions


@pytest.mark.parametrize('reverse', [False, True])
def test_same_url_keeps_reported_summary_before_rule_filtering(reverse):
    stamp = datetime.now(timezone.utc).isoformat()
    empty = EventItem('empty', 'index', '目录', 1, '噪声条例草案报告',
                      'https://example.gov.cn/report', stamp, '', '北京')
    enriched = deepcopy(empty)
    enriched.id = 'enriched'
    enriched.source_id = 'clue_report'
    enriched.summary = '材料陈述：报告指出短时噪声执法取证困难。待核假设：尚需办理样本。'
    before = deepcopy(enriched)
    items = [empty, enriched]
    if reverse:
        items.reverse()
    selected, excluded = rule_screen_with_decisions(items, Settings.load().section('discovery'))
    assert [item.id for item in selected] == ['enriched']
    assert [(x['event'].id, x['reason_code']) for x in excluded] == [('empty', 'duplicate_url')]
    assert enriched.summary == before.summary
    assert enriched.published_at == before.published_at
    assert empty.summary == ''


def test_hypothesis_only_does_not_win_same_url_deduplication():
    stamp = datetime.now(timezone.utc).isoformat()
    first = EventItem('first', 'index', '目录', 1, '噪声条例草案报告',
                      'https://example.gov.cn/report', stamp, '', '北京')
    other = deepcopy(first)
    other.id = 'hypothesis'
    other.summary = '待核假设：存在大量噪声投诉，应完善取证机制。' * 8
    assert deduplicate([first, other], .88) == [first]
    first.summary = '材料陈述：已有调解安排。'
    other.summary = first.summary + other.summary
    assert deduplicate([first, other], .88) == [first]


def test_curated_region_survives_import_queue_and_model_pool(tmp_path):
    base = Settings.load()
    shutil.copytree(base.root / 'config', tmp_path / 'config')
    settings = Settings.load(tmp_path / 'config/settings.toml')
    discovery = LiveDiscovery(settings)
    run_id = discovery.wf.init_run('test_fixture')
    clue_file = tmp_path / 'clues.jsonl'
    clue_file.write_text(json.dumps({
        'title': '老年承租户改造遇到哪些困难',
        'summary': '材料陈述：小户型改造存在空间和价格困难。待核假设：施工手续可能影响选择。',
        'url': 'https://example.test/report',
        'published_at': datetime.now(timezone.utc).isoformat(),
        'source_name': '公开调查', 'source_level': 2,
        'source_region': '北京', 'region': '北京',
        'region_evidence': '原报道明确位于西城区什刹海街道。',
    }, ensure_ascii=False), encoding='utf-8')
    events, _, _ = SourceCollector(tmp_path, settings.raw)._items_from_clue_file(run_id, clue_file)
    screened, _ = rule_screen_with_decisions(events, settings.section('discovery'))
    discovery._enqueue_events(run_id, screened)
    with discovery.wf.db.connect() as conn:
        before = conn.execute('SELECT event_json FROM discovery_queue').fetchone()[0]
    records, _ = discovery._pending_event_records()
    pool = discovery._select_pending_pool(
        records, 12, max_per_source=2, external_reserve=3, fresh_reserve=5, aged_reserve=3)
    assert len(pool) == 1
    assert pool[0].region == '北京'
    assert pool[0].region_evidence == 'clue_import:原报道明确位于西城区什刹海街道。'
    with discovery.wf.db.connect() as conn:
        assert conn.execute('SELECT event_json FROM discovery_queue').fetchone()[0] == before


@pytest.mark.parametrize('evidence', ['text:haidian', 'source_channel_only:北京', 'clue_import:'])
def test_legacy_hypothesis_region_is_still_rechecked(tmp_path, evidence):
    base = Settings.load()
    shutil.copytree(base.root / 'config', tmp_path / 'config')
    discovery = LiveDiscovery(Settings.load(tmp_path / 'config/settings.toml'))
    run_id = discovery.wf.init_run('test_fixture')
    item = EventItem('old', 'source', '调查', 2, '老年改造困难调查',
                     'https://example.test/a', datetime.now(timezone.utc).isoformat(),
                     '材料陈述：老年人反映价格负担。待核假设：海淀可以试点。',
                     '海淀', source_region='北京', region_evidence=evidence)
    rule_screen_with_decisions([item], discovery.s.section('discovery'))
    discovery._enqueue_events(run_id, [item])
    pending, _ = discovery._pending_events()
    assert pending[0].region == '全国'
