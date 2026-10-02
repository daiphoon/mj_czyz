from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil

import pytest

from sqmy.budget import recent_usage
from sqmy.cli import main
from sqmy.config import Settings
from sqmy.conversation_screening import ConversationScreening, digest
from sqmy.discovery import LiveDiscovery
from sqmy.models import EventItem


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    project = Path(__file__).parents[1]
    shutil.copytree(project / 'config', tmp_path / 'config')
    raw = deepcopy(Settings.load(project / 'config/settings.toml').raw)
    raw['model']['provider'] = 'codex_cli'
    raw['shadow_verification']['enabled'] = False
    raw['candidate_eligibility']['mode'] = 'shadow'
    raw['budget']['screening_tokens'] = 1
    settings = Settings(tmp_path, raw)  # UI metadata is unknown; no model ID required.
    discovery = LiveDiscovery(settings)
    titles = ['海淀区住宅维修费用争议', '北京市灵活就业人员职业伤害认定争议',
              '全国未成年人网络消费退款核验争议', '海淀区幼儿园招生资格复核争议',
              '北京市老年人就医结算数据衔接问题', '海淀区中小企业公共数据合规费用问题',
              '北京市租房押金退还拖延纠纷', '海淀区物业公共收益披露核验争议',
              '全国互联网个人信息撤回授权困难', '北京市外卖食品安全重复检查争议']
    events = [EventItem(id=f'event-{n}', source_id=f's-{n}', source_name=f'调查机构{n}', source_level=2,
                        title=title, region='海淀',
                        published_at=datetime.now(timezone.utc).isoformat(),
                        url=f'https://example.test/case/{n}',
                        summary=f'材料陈述：居民{n}办理公共服务结算被要求反复补件，已记录办理结果及相关规则。待核假设：核验流程是否重复。',
                        topics=['基层治理与公共服务']) for n, title in enumerate(titles)]
    collections = []
    def collect(*args, **kwargs):
        collections.append(1)
        return deepcopy(events)
    monkeypatch.setattr('sqmy.discovery.collect_discovery', collect)
    monkeypatch.setattr('sqmy.discovery.build_router', lambda *a, **k: pytest.fail('conversation must not dispatch'))
    monkeypatch.setattr('sqmy.collector.SourceCollector.search_query', lambda *a, **k: pytest.fail('import must not search'))
    run, candidates = discovery.run(screen_now=True, conversation_only=True)
    assert not candidates
    service = ConversationScreening(discovery)
    packet = service._prepared(run)
    assert len(packet['model_event_ids']) >= 8
    return service, run, packet, collections


def answer(schema):
    kind = schema['type']
    if kind == 'object':
        return {k: answer(v) for k, v in schema['properties'].items()}
    if kind == 'string':
        return schema.get('enum', ['待核，有具体核验入口'])[0]
    if kind == 'integer':
        return schema.get('maximum', 0)
    if kind == 'array':
        return [answer(schema['items']) for _ in range(schema.get('minItems', 0))]
    raise AssertionError(kind)


def result_file(context, count=1):
    service, run, packet, _ = context
    envelope = deepcopy(packet['result_envelope'])
    selection = packet['materials']['schema']['properties']['selections']['items']
    items = [answer(selection) for _ in range(count)]
    for item, event_id in zip(items, packet['model_event_ids']):
        item['id'] = event_id
        item['suggested_title'] = f'关于核验社区办理争议{event_id}的建议'
        item['gap_type'] = 'unclear'
        item['gap_hypothesis'] = '待核验具体办理环节的适用条件'
    envelope['result']['selections'] = items
    path = service.s.root / 'conversation-answer.json'
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding='utf-8')
    return path, envelope


def counts(service):
    with service.wf.db.connect() as conn:
        return {name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                for name in ('candidates', 'model_calls', 'stage_usage', 'research_reviews', 'topics')}


def test_zero_candidate_conversation_next_stays_in_conversation(prepared):
    service, run, _, _ = prepared
    path, _ = result_file(prepared, 0)
    service.import_result(run, path)
    state = json.loads(service.wf.status(run, include_all=True)[0]['checkpoint_json'])
    assert 'MODEL_ID' not in state['next']
    assert state['next'] == 'none' or 'scan-prepare' in state['next']


def test_insufficient_conversation_pool_next_uses_scan_prepare(prepared, monkeypatch):
    service, original_run, _, _ = prepared
    answer_path, _ = result_file(prepared, 0)
    service.import_result(original_run, answer_path)
    single = EventItem('one', 'public_report', '调查', 2, '北京市居民住房维修费用争议',
                       'https://example.test/one', datetime.now(timezone.utc).isoformat(),
                       '材料陈述：居民因住房维修收费问题多次投诉，办理结果仍待核验。', '北京')
    monkeypatch.setattr('sqmy.discovery.collect_discovery', lambda *a, **k: [single])
    run, _ = service.discovery.run(screen_now=True, conversation_only=True)
    packet = service._prepared(run)
    path = service.s.root / 'empty-answer.json'
    path.write_text(json.dumps(packet['result_envelope']), encoding='utf-8')
    service.import_result(run, path)
    state = json.loads(service.wf.status(run, include_all=True)[0]['checkpoint_json'])
    assert state['batch_reason'] == 'insufficient_pool_quality'
    assert 'MODEL_ID' not in state['next']
    assert 'scan-prepare' in state['next']


@pytest.mark.parametrize('count', [0, 1, 2, 8])
def test_current_conversation_import_keeps_manual_gates_and_unknown_usage(prepared, count):
    service, run, packet, collected = prepared
    path, _ = result_file(prepared, count)
    _, candidates = service.import_result(run, path)
    expected = min(count, service.s.section('project')['candidate_count'])
    assert len(candidates) == expected
    state = counts(service)
    assert state == dict(candidates=expected, model_calls=0, stage_usage=1, research_reviews=0, topics=0)
    assert len(collected) == 1
    with service.wf.db.connect() as conn:
        usage = conn.execute('SELECT * FROM stage_usage').fetchone()
        assert usage['model'] == 'unknown' and usage['execution_mode'] == 'current_conversation'
        assert usage['accounting_method'] == 'artifact_proxy_estimate'
        assert usage['token_used'] > 1
        assert not conn.execute('SELECT 1 FROM candidates WHERE selected=1').fetchone()
    audits = json.loads((service._directory(run) / 'scan_audits.json').read_text())['audits']
    assert all(item['search_status'] == 'not_searched' for item in audits)
    observed = recent_usage(service.wf.db)
    assert observed['measured_model_tokens'] == 0 and observed['model_calls'] == 0
    checkpoint = json.loads(service.wf.status(run, include_all=True)[0]['checkpoint_json'])
    assert not checkpoint.get('budget_action_required')
    assert checkpoint['conversation_screening']['official_usage'] is None
    assert checkpoint['novelty_live_search'] is False
    for candidate in candidates:
        assert candidate.score > 0 and candidate.eligibility['mode'] == 'shadow'


@pytest.mark.parametrize('mutation', ['unknown_id', 'duplicate_id', 'missing_field', 'score_high', 'score_negative',
                                    'score_bool', 'bad_enum', 'extra_field', 'empty_string', 'query_count',
                                    'duplicate_penalty', 'result_extra', 'envelope_extra', 'run', 'binding'])
def test_invalid_result_is_wholly_rejected_without_candidate_or_usage_writes(prepared, mutation):
    service, run, packet, _ = prepared
    path, envelope = result_file(prepared, 2)
    selected = envelope['result']['selections'][0]
    if mutation == 'unknown_id': selected['id'] = 'not-frozen'
    if mutation == 'duplicate_id': envelope['result']['selections'][1]['id'] = selected['id']
    if mutation == 'missing_field': selected.pop('recommendation')
    if mutation == 'score_high': selected['score_components']['pain_authenticity'] = 999
    if mutation == 'score_negative': selected['score_components']['pain_authenticity'] = -1
    if mutation == 'score_bool': selected['score_components']['pain_authenticity'] = True
    if mutation == 'bad_enum': selected['gap_type'] = 'invented'
    if mutation == 'extra_field': selected['extra'] = 'extra'
    if mutation == 'empty_string': selected['recommendation'] = ' '
    if mutation == 'query_count': selected['counter_queries'] = []
    if mutation == 'duplicate_penalty':
        key = next(iter(service.s.section('penalties')))
        selected['applied_penalties'] = [{'key': key, 'reason': '有依据'}] * 2
    if mutation == 'result_extra': envelope['result']['extra'] = 'extra'
    if mutation == 'envelope_extra': envelope['extra'] = 'extra'
    if mutation == 'run': envelope['run_id'] = 'other'
    if mutation == 'binding': envelope['binding_sha256'] = '0' * 64
    path.write_text(json.dumps(envelope, ensure_ascii=False))
    before = counts(service)
    with pytest.raises(ValueError): service.import_result(run, path)
    assert counts(service) == before
    with service.wf.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM tasks WHERE kind='conversation_screening_import'").fetchone()
        assert not conn.execute("SELECT 1 FROM discovery_queue WHERE status!='pending'").fetchone()


@pytest.mark.parametrize('mutation', ['snapshot', 'schema', 'config', 'expired', 'future', 'contract'])
def test_changed_or_expired_frozen_materials_are_rejected(prepared, mutation):
    service, run, packet, _ = prepared
    path, _ = result_file(prepared)
    directory = service._directory(run)
    if mutation == 'snapshot':
        (directory / 'scan_input.json').write_text('{}')
    elif mutation == 'schema':
        material = json.loads((directory / 'screening_materials.json').read_text())
        material['schema']['required'] = []
        (directory / 'screening_materials.json').write_text(json.dumps(material))
    elif mutation == 'config':
        service.s.raw['scoring']['pain_authenticity'] += 1
    else:
        if mutation == 'expired': packet['binding']['expires_at'] = '2020-01-01T00:00:00+00:00'
        if mutation == 'future': packet['binding']['created_at'] = '2100-01-01T00:00:00+00:00'
        if mutation == 'contract': packet['binding']['contract'] = 'older'
        packet['binding_sha256'] = digest(packet['binding'])
        with service.wf.db.connect() as conn:
            conn.execute("UPDATE tasks SET result_json=? WHERE kind='conversation_screening_prepare'",
                         (json.dumps(packet),))
        envelope = json.loads(path.read_text()); envelope['binding_sha256'] = packet['binding_sha256']
        path.write_text(json.dumps(envelope))
    before = counts(service)
    with pytest.raises(ValueError): service.import_result(run, path)
    assert counts(service) == before


def test_same_result_is_idempotent_and_different_valid_result_cannot_overwrite(prepared):
    service, run, _, _ = prepared
    path, envelope = result_file(prepared)
    service.import_result(run, path)
    service.wf.select(run, ['C1'])
    before = recent_usage(service.wf.db)
    service.import_result(run, path)
    assert recent_usage(service.wf.db)['token_used'] == before['token_used']
    assert counts(service)['candidates'] == 1 and counts(service)['stage_usage'] == 1
    with service.wf.db.connect() as conn:
        assert conn.execute('SELECT selected FROM candidates').fetchone()[0] == 1
    envelope['result']['selections'][0]['recommendation'] = '不同的判断'
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match='不能覆盖'): service.import_result(run, path)
    assert recent_usage(service.wf.db)['token_used'] == before['token_used']


def test_crash_after_candidate_write_resumes_without_duplicate_charge_or_selection(prepared, monkeypatch):
    service, run, packet, collected = prepared
    path, _ = result_file(prepared)
    original = service.discovery._persist_candidates
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('offline interrupted finalization')
    monkeypatch.setattr(service.discovery, '_persist_candidates', crash)
    with pytest.raises(RuntimeError): service.import_result(run, path)
    charged = recent_usage(service.wf.db)['token_used']
    with pytest.raises(ValueError, match='尚未完成'): service.wf.select(run, ['C1'])
    monkeypatch.setattr(service.discovery, '_persist_candidates', original)
    path.unlink()
    _, candidates = service.import_result(run)
    assert len(candidates) == 1 and len(collected) == 1
    assert recent_usage(service.wf.db)['token_used'] == charged
    assert counts(service)['stage_usage'] == 1 and counts(service)['model_calls'] == 0


def test_lock_blocks_concurrent_import(prepared):
    service, run, _, _ = prepared
    path, _ = result_file(prepared)
    with (service.s.root / 'data/runs/.discovery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='正在执行'): service.import_result(run, path)
    assert counts(service)['stage_usage'] == 0


def test_preparation_resume_reuses_frozen_collection_and_rejects_cli_switch(prepared):
    service, run, packet, collected = prepared
    service.discovery.run(resume_run_id=run, conversation_only=True)
    assert service._prepared(run) == packet and len(collected) == 1
    with pytest.raises(ValueError): service.discovery.run(resume_run_id=run)


def test_failure_before_prepared_packet_keeps_conversation_resume_mode(prepared, monkeypatch):
    service, _, _, collected = prepared
    original = ConversationScreening.prepare
    monkeypatch.setattr(ConversationScreening, 'prepare', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline crash before packet')))
    with pytest.raises(RuntimeError): service.discovery.run(conversation_only=True, screen_now=True)
    run = service.discovery._active_run_id
    checkpoint = json.loads(service.wf.status(run, include_all=True)[0]['checkpoint_json'])
    assert checkpoint['conversation_only'] is True and checkpoint['selected_model'] is None
    assert 'scan-prepare --resume' in checkpoint['resume_next']
    before = len(collected)
    monkeypatch.setattr(ConversationScreening, 'prepare', original)
    service.discovery.run(resume_run_id=run, conversation_only=True)
    assert len(collected) == before and service._prepared(run)['execution_mode'] == 'current_conversation'


def test_json_duplicate_keys_and_nonfinite_values_rejected(prepared):
    service, run, _, _ = prepared
    path, _ = result_file(prepared, 0)
    text = path.read_text()
    path.write_text(text.replace('"selections": []', '"selections": [], "selections": []'))
    with pytest.raises(ValueError, match='重复字段'): service.import_result(run, path)
    path.write_text(text.replace('"selections": []', '"selections": [NaN]'))
    with pytest.raises(ValueError, match='非有限'): service.import_result(run, path)


@pytest.mark.parametrize('kind', ['review_stop', 'no_draft'])
def test_stop_record_added_after_prepare_blocks_import(prepared, kind):
    service, run, packet, _ = prepared
    path, _ = result_file(prepared)
    state = json.loads((service._directory(run) / 'scan_input.json').read_text())
    event = next(x for x in state['model_pool'] if x['id'] == packet['model_event_ids'][0])
    old = service.wf.init_run('live')
    candidate = {'title': event['title'], 'summary': event['summary'], 'score_reasons': {'来源URL': event['url']}}
    with service.wf.db.connect() as conn:
        conn.execute('INSERT INTO candidates(id,run_id,title,data_json,score,created_at) VALUES(?,?,?,?,70,?)',
                     (f'{old}:C1', old, event['title'], json.dumps(candidate), datetime.now(timezone.utc).isoformat()))
        payload = {'candidate_id': 'C1', 'topic_id': 'stopped', 'working_title': event['title'], 'sources': [],
                   'stop_summary': '决定性前提不成立', 'reopen_condition': '须取得新证据'}
        if kind == 'review_stop':
            conn.execute("INSERT INTO research_reviews(id,run_id,candidate_id,topic_id,input_hash,decision,confidence,research_allowed,data_json,report_path,created_at) VALUES('new-stop',?,'C1','stopped','new','stop','medium',0,?,'unused',?)",
                         (old, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
        else:
            payload.update(outcome='no_draft', conclusion='决定性前提不成立')
            conn.execute("INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at) VALUES('new-no-draft',?,'research_brief','new','completed',?,?)",
                         (old, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
    with pytest.raises(ValueError, match='停止或重开'): service.import_result(run, path)
    assert counts(service)['stage_usage'] == 0


def test_cli_import_and_prepare_need_no_model_identifier(prepared, monkeypatch, capsys):
    service, run, _, collected = prepared
    monkeypatch.setattr('sqmy.cli.Settings.load', lambda *a: service.s)
    assert main(['scan-prepare', '--resume', run]) == 0
    assert len(collected) == 1
    path, _ = result_file(prepared, 0)
    monkeypatch.setattr('sqmy.cli.load_dotenv', lambda *a, **k: pytest.fail('import does not need credentials'))
    assert main(['scan-import', run, '--result', str(path)]) == 0
    output = capsys.readouterr().out
    assert 'current_conversation' in output and 'unknown' in output
    assert '真实服务调用0次' in output


@pytest.mark.parametrize('point', ['usage', 'checkpoint'])
def test_crash_recovery_uses_accepted_database_result(prepared, monkeypatch, point):
    service, run, _, _ = prepared
    path, _ = result_file(prepared)
    if point == 'usage':
        from sqmy import conversation_screening as module
        original = module.record_stage_usage
        monkeypatch.setattr(module, 'record_stage_usage', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline crash')))
    else:
        original = service.discovery._finish_scan
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('offline crash')
        monkeypatch.setattr(service.discovery, '_finish_scan', crash)
    with pytest.raises(RuntimeError): service.import_result(run, path)
    if point == 'usage': monkeypatch.setattr(module, 'record_stage_usage', original)
    else:
        service.wf.select(run, ['C1'])
        monkeypatch.setattr(service.discovery, '_finish_scan', lambda *a, **k: pytest.fail('must not rewrite finished candidates'))
    path.unlink()
    _, candidates = service.import_result(run)
    assert len(candidates) == 1 and counts(service)['stage_usage'] == 1
    if point == 'checkpoint':
        with service.wf.db.connect() as conn:
            assert conn.execute('SELECT selected FROM candidates').fetchone()[0] == 1
