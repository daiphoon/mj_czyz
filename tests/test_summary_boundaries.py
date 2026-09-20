from copy import deepcopy
from datetime import datetime, timezone
import json
import shutil
import tomllib

import pytest

from sqmy.collector import SourceCollector, infer_event_region
from sqmy.config import Settings
from sqmy.models import EventItem
from sqmy.screener import classify, problem_priority, rule_screen_with_decisions


def event(summary, title='386路公交车体验'):
    return EventItem('case', 's', '公开答复', 1, title, 'https://example.test/a',
                     datetime.now(timezone.utc).isoformat(), summary, '北京')


@pytest.mark.parametrize('hypothesis', [
    '待核假设：尚缺独立客流数据，存在机制不足。',
    '原因推测：政策风险和数据问题，建议征求意见。',
    '缺口假设（待核）：医院纠纷需要投诉和调查。',
])
def test_hypotheses_cannot_change_rule_selection_score_or_topics(hypothesis):
    cfg = Settings.load().section('discovery')
    plain = event('材料陈述：公交集团答复已调阅视频，并将按客流调整运力。')
    enriched = deepcopy(plain)
    enriched.summary += hypothesis
    before = enriched.summary
    a, _ = rule_screen_with_decisions([plain], cfg)
    b, _ = rule_screen_with_decisions([enriched], cfg)
    assert bool(a) == bool(b)
    assert enriched.topics == plain.topics
    assert enriched.rule_score == plain.rule_score
    assert problem_priority(enriched, cfg) == problem_priority(plain, cfg)
    assert enriched.summary == before


def test_hypothesis_cannot_supply_problem_signal_or_promotion_exception():
    cfg = Settings.load().section('discovery')
    item = event('材料陈述：医院提供常规服务。待核假设：存在投诉纠纷。', '医院服务介绍')
    selected, excluded = rule_screen_with_decisions([item], cfg)
    assert not selected
    assert excluded[0]['reason_code'] == 'no_problem_or_evidence_signal'
    activity = event('材料陈述：介绍社区服务。待核假设：投诉退费纠纷，拟征求意见。', '社区会议召开')
    assert problem_priority(activity, cfg) == 0


def test_legacy_unmarked_and_inline_quoted_material_remains_available():
    item = event('部门公开调查养老服务问题；报道将其称为“待核假设：机制不足”。')
    assert '养老与医疗' in classify(item)
    assert rule_screen_with_decisions([item], Settings.load().section('discovery'))[0]


def test_hypothesis_cannot_change_region():
    assert infer_event_region('公交体验', '材料陈述：运营方作出答复。待核假设：海淀可试点。',
                              'https://example.test/a', '全国')[0] == '全国'


def test_public_reply_directory_does_not_claim_reply_date():
    settings = Settings.load()
    sources = tomllib.loads((settings.root / 'config/sources.toml').read_text())['sources']
    source = next(s for s in sources if s['id'] == 'beijing_public_replies')
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    html = f'<li><a href="/hudong/hdjl/test">386路公交车体验</a><span>{stamp}</span></li>'
    item = SourceCollector(settings.root, settings.raw)._items_from_payloads([{'source': source, 'xml': html}])[0]
    assert item.material['discovery_provenance']['date_basis'] == 'listing_displayed_date'
    assert item.material['event_at'] == ''
    assert '答复' in item.material['date_note']
    assert 'reply_at' not in item.material


def test_old_pending_hypotheses_are_rechecked_without_rewriting_queue(tmp_path):
    from sqmy.discovery import LiveDiscovery
    settings = Settings.load()
    shutil.copytree(settings.root / 'config', tmp_path / 'config')
    discovery = LiveDiscovery(Settings.load(tmp_path / 'config/settings.toml'))
    run = discovery.wf.init_run('test_fixture')
    old = event('材料陈述：运营方已答复。待核假设：海淀数据治理存在问题。')
    old.rule_score = 99
    old.topics = ['数据治理']
    discovery._enqueue_events(run, [old])
    with discovery.wf.db.connect() as conn:
        before = conn.execute('SELECT event_json FROM discovery_queue').fetchone()[0]
    assert discovery._pending_events()[0] == []
    with discovery.wf.db.connect() as conn:
        after = conn.execute('SELECT event_json FROM discovery_queue').fetchone()[0]
    assert before == after
    assert json.loads(after)['rule_score'] == 99


def test_hypothesis_cannot_trigger_urgent_exception():
    from sqmy.discovery import LiveDiscovery
    # 不初始化真实工作流数据库；这个方法只读取配置。
    discovery = object.__new__(LiveDiscovery)
    discovery.s = Settings.load()
    signal = discovery.s.section('discovery')['urgent_keywords'][0]
    item = event('材料陈述：运营方已答复。待核假设：' + signal)
    item.rule_score = 100
    ready, reason = discovery._should_run_model([item], force=False, quality_report={'ok': False})
    assert not ready
    assert reason == 'insufficient_pool_quality'
