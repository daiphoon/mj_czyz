from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from sqmy.config import Settings
from sqmy.db import now
from sqmy.discovery import LiveDiscovery
from sqmy.models import EventItem
from sqmy.research_gate import review_pre_research
from sqmy.research_stops import ResearchStopGate, event_signature, reopen_errors


@pytest.fixture
def stopped(tmp_path):
    raw = deepcopy(Settings.load(Path(__file__).parents[1] / 'config/settings.toml').raw)
    discovery = LiveDiscovery(Settings(tmp_path, raw).with_model("offline-test-model"))
    old = discovery.wf.init_run('live')
    current = discovery.wf.init_run('live')
    event = EventItem('case', 'paper', '原调查', 2, '社区充电居民电价办理问题调查',
        'https://paper.test/original', now(), '社区充电收费调整申请，工程条件具备后反复补交材料，调查报道未给受理节点。', '北京')
    payload = {'candidate_id': 'M1', 'topic_id': 'stopped-topic', 'working_title': event.title,
        'decision': 'stop', 'stop_summary': '未取得工程条件具备后的受理与转办节点',
        'reopen_condition': '取得工程具备条件、完整提交材料、受理转办时点的可核验记录',
        'sources': [{'key': 'old', 'url': event.url, 'source_role': 'media_investigation',
            'excerpt': event.summary, 'source_level': 2, 'fetch_status': 'excerpt_verified', 'locator': '调查办理段'}]}
    with discovery.wf.db.connect() as conn:
        conn.execute('INSERT INTO candidates(id,run_id,title,data_json,score,selected,created_at) VALUES(?,?,?,?,0,1,?)',
            (old + ':M1', old, event.title, json.dumps({'title': event.title, 'summary': event.summary,
             'score_reasons': {'来源URL': event.url}}), '2026-09-28T00:00:00+00:00'))
        conn.execute('''INSERT INTO research_reviews(id,run_id,candidate_id,topic_id,input_hash,decision,confidence,
            research_allowed,data_json,report_path,created_at) VALUES('stop-review',?,'M1','stopped-topic','stop-hash','stop','medium',0,?,'unused','2026-09-28T00:00:00+00:00')''',
            (old, json.dumps(payload)))
    return discovery, old, current, event, payload


def test_changed_title_cannot_spend_a_model_call(stopped, monkeypatch):
    discovery, old, current, event, _ = stopped
    renamed = replace(event, title='居民电价社区办理的另一个标题')
    def forbidden(*args, **kwargs):
        pytest.fail('停止题到达了提供商入口')
    monkeypatch.setattr('sqmy.discovery.build_router', forbidden)
    assert discovery._model_rank(current, [renamed]) == ([], False, 0)
    with discovery.wf.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0
    audit = json.loads((discovery.s.root / 'data/runs' / current / 'research_stop_gate.json').read_text())
    assert audit['decisions'][0]['stop_ids'] == ['review:stop-review']


def test_repost_url_and_known_origin_remain_stopped(stopped):
    discovery, _, current, event, _ = stopped
    repost = replace(event, url='https://repost.test/new', title='转载的办理调查',
                     material={'original_source_hint': event.url})
    allowed, blocked = discovery._partition_research_stops([repost], current)
    assert not allowed and blocked[0]['reason_code'] == 'research_stop'
    same_text = replace(event, url='https://repost.test/second')
    assert not ResearchStopGate(discovery.wf.db).partition([same_text])[0]


def test_force_does_not_bypass_stop_and_queue_pending_is_filtered(stopped):
    discovery, old, current, event, _ = stopped
    prepared = discovery._prepare_tier_pool([event], current, mode='live', force=True)
    assert prepared['rule_results']
    assert not prepared['fresh_results']
    assert prepared['history_exclusions'][0]['reason_code'] == 'research_stop'
    discovery._enqueue_events(old, [event])
    records, _ = discovery._pending_event_records()
    assert records
    assert not discovery._partition_research_stops([e for e, _ in records], current)[0]


def test_manual_stop_survives_later_edit_to_same_review(stopped):
    discovery, old, _, event, _ = stopped
    with discovery.wf.db.connect() as conn:
        conn.execute("UPDATE research_reviews SET decision='reframe',research_allowed=1 WHERE id='stop-review'")
    review_pre_research(discovery.s, old, 'M1', decision='stop', note='用户停止，等待新节点证据')
    with discovery.wf.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='research_stop'").fetchone()[0] == 1
        conn.execute("UPDATE research_reviews SET human_decision='proceed' WHERE id='stop-review'")
    allowed, _, decisions = ResearchStopGate(discovery.wf.db).partition([event])
    assert not allowed and decisions[0]['stop_summary'] == ['用户停止，等待新节点证据']


def test_no_draft_brief_is_read_without_topics_row(stopped):
    discovery, old, _, event, payload = stopped
    with discovery.wf.db.connect() as conn:
        conn.execute("UPDATE research_reviews SET decision='reframe',research_allowed=1,human_decision='proceed' WHERE id='stop-review'")
        brief = {'candidate_id': 'M1', 'topic_id': 'stopped-topic', 'pre_research_review_id': 'stop-review',
            'outcome': 'no_draft', 'conclusion': '不能由总工期解释办理空转', 'reopen_condition': payload['reopen_condition']}
        conn.execute("INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at) VALUES('brief-stop',?,'research_brief','brief-hash','completed',?,'2026-09-29T00:00:00+00:00')", (old, json.dumps(brief)))
    allowed, _, decisions = ResearchStopGate(discovery.wf.db).partition([event])
    assert not allowed and decisions[0]['stop_ids'] == ['brief:brief-stop']


def add_reopen(stopped, event, *, approved=True, unread=False, old_source=False):
    discovery, old, _, original, payload = stopped
    source = deepcopy(payload['sources'][0]) if old_source else {
        'key': 'node', 'url': 'https://authority.test/nodes', 'source_level': 1,
        'fetch_status': 'excerpt_verified', 'locator': '受理、材料齐备与移交日期段',
        'excerpt': '工程条件已具备、材料完整提交后，记录显示两次重复移交及各次受理时点。'}
    if unread:
        source['fetch_status'] = 'summary_only'
    record = {'sources': [source], 'verified_facts': [{'id': 'F1', 'statement': source['excerpt'], 'source_keys': [source['key']]}],
        'reopen_assessment': {'stop_ids': ['review:stop-review'], 'event_content_sha256': event_signature(event),
            'basis': 'new_evidence', 'condition_met': '记录区分工程具备条件前后耗时，并定位重复转办', 'new_evidence_claim_ids': ['F1']}}
    with discovery.wf.db.connect() as conn:
        conn.execute('''INSERT INTO research_reviews(id,run_id,candidate_id,topic_id,input_hash,decision,confidence,
            research_allowed,data_json,report_path,human_decision,reviewed_at,created_at)
            VALUES('new-review',?,'M1','stopped-topic','new-hash','proceed','medium',1,?,'unused',?,'2026-09-30T01:00:00+00:00','2026-09-30T00:00:00+00:00')''',
            (old, json.dumps(record), 'proceed' if approved else None))
    return record


def test_decisive_new_read_evidence_and_new_human_review_can_reopen(stopped):
    discovery, _, current, event, _ = stopped
    changed = replace(event, summary=event.summary + ' 新节点记录已取得。')
    add_reopen(stopped, changed)
    allowed, blocked, decisions = ResearchStopGate(discovery.wf.db).partition([changed])
    assert allowed == [changed] and not blocked
    assert decisions[0]['reopen_review_ids'] == ['new-review']
    assert changed.material['research_history']['status'] == 'reopened'
    # 明确重开可以通过未变化历史层，不能让旧去重再次永久封禁。
    discovery._persist_events(current, [changed])
    another = discovery.wf.init_run('live')
    discovery._partition_research_stops([changed], another)
    assert discovery._partition_unchanged([changed], another, 'live')[0] == [changed]


@pytest.mark.parametrize('options', [{'approved': False}, {'unread': True}, {'old_source': True}])
def test_reopen_requires_approval_read_context_and_actual_new_material(stopped, options):
    discovery, _, _, event, _ = stopped
    changed = replace(event, summary=event.summary + ' 一段新写的摘要。')
    add_reopen(stopped, changed, **options)
    assert not ResearchStopGate(discovery.wf.db).partition([changed])[0]


def test_reopen_is_bound_to_exact_evidence_version_and_unrelated_topics_are_allowed(stopped):
    discovery, _, _, event, _ = stopped
    add_reopen(stopped, event)
    different = replace(event, summary=event.summary + ' 未审核的另一个原因。')
    assert not ResearchStopGate(discovery.wf.db).partition([different])[0]
    unrelated = replace(event, title='社区充电设备产品安全认证抽检', url='https://authority.test/safety', summary='新的产品安全抽检项目和认证适用问题。')
    assert ResearchStopGate(discovery.wf.db).partition([unrelated])[0] == [unrelated]


def test_fixture_stop_records_do_not_block_live_discovery(stopped):
    discovery, old, _, event, _ = stopped
    with discovery.wf.db.connect() as conn:
        conn.execute("UPDATE run_context SET mode='test_fixture' WHERE run_id=?", (old,))
    assert ResearchStopGate(discovery.wf.db).partition([event])[0] == [event]


def test_reopen_contract_does_not_accept_url_only_or_unsupported_claim(stopped):
    _, _, _, event, _ = stopped
    record = add_reopen(stopped, event)
    record['sources'][0]['locator'] = ''
    assert reopen_errors(record)
    record['sources'][0]['locator'] = '办理段'
    record['reopen_assessment']['new_evidence_claim_ids'] = ['invented']
    assert reopen_errors(record)


def test_explicit_correction_with_new_human_review_can_use_original_source(stopped):
    discovery, _, _, event, _ = stopped
    record = add_reopen(stopped, event, old_source=True)
    record['reopen_assessment'].update(basis='correction', correction_reason='此前遗漏了已读原文中的必要办理时点，重新核对定位后纠正停止前提')
    assert not reopen_errors(record)
    with discovery.wf.db.connect() as conn:
        conn.execute("UPDATE research_reviews SET data_json=? WHERE id='new-review'", (json.dumps(record),))
    assert ResearchStopGate(discovery.wf.db).partition([event])[0] == [event]


def test_later_manual_stop_invalidates_earlier_reopen(stopped):
    discovery, _, _, event, _ = stopped
    add_reopen(stopped, event)
    with discovery.wf.db.connect() as conn:
        conn.execute("UPDATE research_reviews SET human_decision='stop',human_note='新条件仍不足',reviewed_at='2026-10-01T00:00:00+00:00' WHERE id='new-review'")
    assert not ResearchStopGate(discovery.wf.db).partition([event])[0]


def test_frozen_prompt_with_stopped_item_is_not_reused(stopped, monkeypatch):
    discovery, _, current, event, _ = stopped
    other = replace(event, id='other', title='养老机构服务收费问题调查', url='https://authority.test/elder', summary='新的养老机构收费问题。')
    path=discovery.s.root/'data/runs'/current/'screening_materials.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'model_event_ids':[event.id,other.id],'prompt':'包含旧停止题的冻结提示'}))
    monkeypatch.setattr('sqmy.discovery.build_router', lambda *args: pytest.fail('旧冻结提示调用了提供商'))
    with pytest.raises(ValueError, match='冻结提示'):
        discovery._model_rank(current, [event,other])
