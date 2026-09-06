from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqmy.config import Settings
from sqmy.discovery import LiveDiscovery
from sqmy.history_match import mechanism_hints
from sqmy.models import EventItem
from sqmy.screener import problem_priority, rule_screen_with_decisions


def test_promotional_problem_title_is_deprioritized_not_deleted(tmp_path):
    cfg = settings_at(tmp_path).section('discovery')
    advert = event('ad', '培训退费陷阱调查', '机构参考：某校课程报名服务')
    advert.material = {'promotion_suspected': True}
    report = event('report', '培训退款陷阱调查追踪', '消费者投诉招生广告，记者采访')
    report.material = {'promotion_suspected': True}
    assert problem_priority(advert, cfg) < problem_priority(report, cfg)
    selected, _ = rule_screen_with_decisions([advert], cfg)
    assert selected  # 排序而非新增硬排除


def test_repost_compaction_preserves_originals_and_changed_facts():
    from sqmy.materials import compact_reposts
    original = event('original', '监管部门专项行动第二阶段', '监管部门发布调查结果。' * 15 + '查处561件。')
    original.source_level = 1
    repost = event('repost', '某专项行动进展', '记者获悉，' + original.summary)
    changed = event('changed', '某专项行动新调查', original.summary.replace('561', '562'))
    pool, merged = compact_reposts([repost, original, changed])
    assert {x.id for x in pool} == {'original', 'changed'}
    assert merged == {'original': ['repost']}
    assert not original.material and not repost.material


def test_pending_pool_does_not_refill_with_repost(tmp_path):
    d = LiveDiscovery(settings_at(tmp_path))
    first = event('a', '监管调查收费问题', '监管发布具体调查结果，发现退费争议。' * 12)
    first.source_level = 1
    second = event('b', '记者报道收费纠纷', '记者获悉，' + first.summary)
    records = [(x, datetime.now(timezone.utc)) for x in [second, first]]
    pool = d._select_pending_pool(records, 12, max_per_source=2, external_reserve=3, fresh_reserve=5, aged_reserve=3)
    assert [x.id for x in pool] == ['a']


def test_source_diversity_report_does_not_claim_independent_evidence(tmp_path):
    d = LiveDiscovery(settings_at(tmp_path))
    report = d._report('fixture', [], [], 0, 0, 0, 0, False, 0, 0, 'fixture', 1, 4,
                       pool_quality={'checked': True, 'ok': True, 'source_count': 3})
    text = report.read_text()
    assert '来源标识 3 个（不代表独立原始信息链）' in text
    assert '独立来源 3 个' not in text


def settings_at(tmp_path):
    return Settings(tmp_path, deepcopy(Settings.load(Path(__file__).parents[1] / 'config/settings.toml').raw))


def event(id, title, summary='', score=70, tier=3):
    return EventItem(id, id, id, 2, title, f'https://example.test/{id}',
                     datetime.now(timezone.utc).isoformat(), summary, '全国',
                     rule_score=score, expansion_tier=tier)


def test_mechanism_hint_catches_changed_title_but_not_generic_governance(tmp_path):
    cfg = settings_at(tmp_path).section('history_review')
    history = [dict(id='old', title='关于完善医疗器械网络广告动态核验机制的建议',
                    affected_group='医疗器械消费者', core_problem='广告审批数据与平台投放信息错配',
                    mechanism_entry='平台核验广告审查文号和医疗器械注册适用范围，变更后复核',
                    recommendation_summary='结构化核验与申诉纠错')]
    hints = mechanism_hints('AI虚拟数字人宣传理疗贴疗效，平台校验医疗器械注册适用范围和广告审查文号', history, cfg)
    assert hints[0]['topic_id'] == 'old'
    assert '不是自动排除' in hints[0]['instruction']
    assert not mechanism_hints('社区投诉平台应完善信息公开、监督管理和纠错机制', history, cfg)


def test_promotional_titles_with_real_problem_or_window_remain_eligible(tmp_path):
    cfg = settings_at(tmp_path).section('discovery')
    plain = event('plain', '中小企业专题讲座', '企业治理数据介绍')
    problem = event('problem', '中小企业专题讲座讨论拖欠账款问题', '调查发现投诉困难')
    window = event('window', '会议发布中小企业政策征求意见', '企业融资办法')
    assert problem_priority(plain, cfg) == 0
    assert problem_priority(problem, cfg) == 2
    assert problem_priority(window, cfg) == 2
    selected, _ = rule_screen_with_decisions([plain, problem, window], cfg)
    assert {problem.id, window.id} <= {x.id for x in selected}
    assert problem_priority(event('exam', '结构化面试：市民投诉问题', '投诉案例'), cfg) == 0
    assert problem_priority(EventItem('std', 's', 's', 1, '陪诊国标实施衔接', 'https://example.test', '', '', '全国'), cfg) == 2


def test_current_import_uses_existing_external_quota_not_extra_slots(tmp_path):
    s = settings_at(tmp_path)
    d = LiveDiscovery(s)
    now = datetime.now(timezone.utc)
    records = [(event(f'old{i}', f'历史平台收费问题{i}', score=99, tier=4), now-timedelta(days=4)) for i in range(6)]
    records += [(event(f'new{i}', f'本次平台退款问题{i}', score=65, tier=4), now) for i in range(3)]
    records += [(event(f'promo{i}', f'企业专题讲座{i}', score=100), now) for i in range(6)]
    selected = d._select_pending_pool(records, 8, max_per_source=2, external_reserve=3,
                                      fresh_reserve=3, aged_reserve=2,
                                      current_clue_ids={'new0', 'new1', 'new2'})
    assert len(selected) == 8
    assert len({'new0', 'new1', 'new2'} & {x.id for x in selected}) >= 2
    assert sum(x.id.startswith('old') for x in selected) >= 2


def test_test_fixture_approved_topics_do_not_enter_history_hints(tmp_path):
    d = LiveDiscovery(settings_at(tmp_path))
    run = d.wf.init_run('test_fixture')
    with d.wf.db.connect() as c:
        c.execute("INSERT INTO topics(id,title,created_at,run_id,approval_status) VALUES('fixture','测试','2026-09-06',?,'approved')", (run,))
    assert not d._approved_history()
