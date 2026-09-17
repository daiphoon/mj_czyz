from copy import deepcopy
import json

import pytest

from sqmy.budget import record_stage_usage, recent_usage
from sqmy.delivery import evidence_fingerprint
from sqmy.providers import ProviderError
from sqmy.semantic_review import semantic_review, prepare_input, current_review, register_adjudication, validate_output
from test_discovery import FakeRouter
from test_delivery_quality import draft_context
from test_claim_evidence_v2 import detailed_package


@pytest.fixture
def v3_context(draft_context):
    # 已批准研究、尚未导入新版证据的边界；不删除或降低已经记账的用量。
    return draft_context


def material_package(context):
    settings, wf, _, _ = context
    data = detailed_package(); data['topic_id'] = 'delivery-topic'
    for source in data['sources']:
        source['recommendation'] = '不得进入审查模型的作者推荐'
    for c in data['claims']:
        c['confidence'] = '不得进入模型的作者置信度'
        for s in c['sources']:
            s['evidence_detail']['context'] = '原材料表注：仅本地区当年登记样本，未包括退出对象。'
    return data


def save_package(context, data=None):
    settings, _, _, _ = context
    path = settings.root / 'semantic-package.json'
    path.write_text(json.dumps(data or material_package(context), ensure_ascii=False))
    return path


def response(prepared):
    material = prepared['material']
    return {
        'relations': [dict(claim_id=c['id'], source_ref=s['source_ref'], relationship='partially_supports',
                           issue_codes=['population'], reason='仅覆盖一部分总体') for c in material['claims'] for s in c['sources']],
        'claims': [dict(claim_id=c['id'], source_refs=[s['source_ref'] for s in c['sources']], relationship='cannot_determine',
                        issue_codes=['population'], reason='尚无法确认整体覆盖') for c in material['claims']],
    }


def enable(context, monkeypatch, data=None):
    settings, wf, run, _ = context
    settings.raw['semantic_review']['enabled'] = True
    with wf.db.connect() as conn:
        conn.execute("UPDATE run_context SET mode='live' WHERE run_id=?", (run,))
    package = data or material_package(context)
    router = FakeRouter(response(prepare_input(settings, package)))
    monkeypatch.setattr('sqmy.semantic_review.build_router', lambda *a, **k: router)
    return router, save_package(context, package)


def test_prepare_uses_original_context_not_author_conclusions_and_never_calls(v3_context, monkeypatch):
    settings, _, run, _ = v3_context
    monkeypatch.setattr('sqmy.semantic_review.build_router', lambda *a, **k: pytest.fail('不可调用'))
    prepared = semantic_review(settings, run, 'delivery-topic', package_path=save_package(v3_context))
    text = json.dumps(prepared, ensure_ascii=False)
    assert '未包括退出对象' in text
    assert '不得进入' not in text
    assert 'support_scope' not in text and 'confidence' not in text
    assert prepared['status'] == 'prepared'
    with pytest.raises(ValueError, match='尚未启用'):
        semantic_review(settings, run, 'delivery-topic', package_path=save_package(v3_context), execute=True)


def test_paraphrase_or_unread_never_becomes_a_semantic_fact_verdict(v3_context):
    settings, _, _, _ = v3_context
    for kind in ('paraphrase', 'unavailable'):
        data = material_package(v3_context)
        detail = data['claims'][0]['sources'][0]['evidence_detail']
        detail.update(excerpt_kind=kind, fetch_status='access_failed')
        assert prepare_input(settings, data)['status'] == 'review_unavailable'


def test_scope_change_invalidates_cache_but_author_confidence_does_not(v3_context, monkeypatch):
    settings, wf, run, _ = v3_context
    router, path = enable(v3_context, monkeypatch)
    before = evidence_fingerprint(wf.db, 'delivery-topic')
    first = semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    assert first['status'] == 'completed'
    second = semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    assert second == first and router.calls == 1
    assert evidence_fingerprint(wf.db, 'delivery-topic') == before
    package = json.loads(path.read_text()); package['claims'][0]['confidence'] = 'new'
    path.write_text(json.dumps(package))
    assert semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True) == first
    package['claims'][0]['scope']['geography'] = '更窄范围'
    assert prepare_input(settings, package)['material_sha256'] != first['material_sha256']
    with wf.db.connect() as conn:
        assert conn.execute("SELECT human_decision FROM research_reviews").fetchone()[0] == 'proceed'


def test_same_stage_cap_includes_measured_and_interactive_reservations(v3_context, monkeypatch):
    settings, wf, run, _ = v3_context
    router, path = enable(v3_context, monkeypatch)
    semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    record_stage_usage(wf.db, run_id=run, topic_id='delivery-topic', stage='deep_research', token_used=40000,
                       input_hash='boundary', provider='codex_subscription', model='test', note='测试耐久边界')
    totals = recent_usage(wf.db)
    assert totals['measured_model_tokens'] == 120
    assert totals['estimated_interactive_tokens'] == 39880
    package = json.loads(path.read_text()); package['claims'][0]['claim_text'] += '新增范围'
    path.write_text(json.dumps(package))
    blocked = semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    assert blocked['status'] == 'review_unavailable' and blocked['failure_code'] == 'BudgetExceeded'
    assert router.calls == 1
    assert recent_usage(wf.db)['token_used'] == 40000
    with wf.db.connect() as conn:
        assert conn.execute('SELECT status FROM runs WHERE id=?', (run,)).fetchone()[0] == 'paused_budget'


def test_tool_failure_is_unavailable_keeps_reservation_and_retry_stays_bounded(v3_context, monkeypatch):
    settings, wf, run, _ = v3_context
    router, path = enable(v3_context, monkeypatch)
    def fail(*a, **k):
        raise ProviderError('测试网络失败')
    # 在实际 ledger 的 client 边界失败，不能用绕过账本的 router 异常伪测记账。
    from sqmy.providers import ProviderRouter
    class Client:
        provider = 'codex_cli'; model = 'test'
        analyze = fail
    monkeypatch.setattr('sqmy.semantic_review.build_router', lambda *a, **k: ProviderRouter(Client(), None, []))
    first = semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    assert first['status'] == 'review_unavailable' and 'assessment' not in first
    used = recent_usage(wf.db)['estimated_unconfirmed_model_tokens']
    assert used > 0
    assert semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True) == first
    # 无论后续重试被Token还是次数闸门停止，未知用量都不能清零。
    semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True, retry=True)
    assert recent_usage(wf.db)['estimated_unconfirmed_model_tokens'] >= used


def test_missing_relationship_or_combination_is_invalid(v3_context):
    settings, _, _, _ = v3_context
    prepared = prepare_input(settings, material_package(v3_context))
    output = response(prepared)
    validate_output(output, prepared['material'])
    output['relations'].pop()
    with pytest.raises(ProviderError, match='来源关系'):
        validate_output(output, prepared['material'])


def test_review_survives_evidence_import_and_adjudication_does_not_change_claims(v3_context, monkeypatch):
    from test_evidence_safety import ingest
    settings, wf, run, _ = v3_context
    router, path = enable(v3_context, monkeypatch)
    result = semantic_review(settings, run, 'delivery-topic', package_path=path, execute=True)
    ingest(settings, json.loads(path.read_text()))
    assert current_review(settings, 'delivery-topic', run)['review_sha256'] == result['review_sha256']
    before = evidence_fingerprint(wf.db, 'delivery-topic')
    record = dict(review_sha256=result['review_sha256'], reviewer='fixture', reviewed_at=result['reviewed_at'],
                  decisions=[dict(claim_id='c1', action='narrow', reason='须缩小主张', source_check='核对表注，样本有排除项')])
    target = settings.root / 'adjudication.json'; target.write_text(json.dumps(record))
    register_adjudication(settings, run, 'delivery-topic', target)
    assert evidence_fingerprint(wf.db, 'delivery-topic') == before
