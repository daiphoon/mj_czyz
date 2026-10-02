from datetime import datetime, timedelta, timezone
from pathlib import Path
from copy import deepcopy
import json
import shutil
from unittest.mock import patch
import pytest

from delivery_helpers import valid_text, record_valid_review, review_record
from test_evidence_safety import package, ingest
from sqmy.delivery import check_draft, register_review, require_review
from sqmy.workflow import Workflow
from sqmy.collector import SourceCollector, listing_to_rss, canonical_url

from sqmy.config import Settings
from sqmy.db import Database
from sqmy.models import EventItem
from sqmy.novelty import production_funnel
from sqmy.screener import deduplicate


def settings_at(tmp_path):
    return Settings(tmp_path, deepcopy(Settings.load(Path(__file__).parents[1] / "config/settings.toml").raw))


def test_distinct_article_query_ids_survive_deduplication():
    first = EventItem("a", "s", "s", 1, "医院收费申诉流程", "https://example.gov.cn/view?id=1", "", "", "全国")
    second = EventItem("b", "s", "s", 1, "农田水利管理纠纷", "https://example.gov.cn/view?id=2", "", "", "全国")
    assert len(deduplicate([first, second], 0.9)) == 2


def test_four_evidence_flags_without_documents_never_mean_stable_output(tmp_path):
    settings = settings_at(tmp_path)
    db = Database(settings.database_path)
    db.initialize()
    reference = datetime(2026, 9, 6, tzinfo=timezone.utc)
    with db.connect() as conn:
        for i in range(1, 5):
            stamp = (reference - timedelta(weeks=i)).isoformat()
            conn.execute("INSERT INTO runs(id,phase,status,config_hash,created_at,updated_at,checkpoint_json) VALUES(?,?,?,?,?,?,?)",
                         (str(i), "research", "completed", "x", stamp, stamp, json.dumps({"evidence_gate": "passed"})))
            conn.execute("INSERT INTO run_context(run_id,mode,created_at) VALUES(?,?,?)", (str(i), "live", stamp))
    report = production_funnel(settings, reference=reference)
    assert report["stable_minimum_output"] is False


@pytest.fixture
def draft_context(tmp_path):
    settings = settings_at(tmp_path)
    root = Path(__file__).parents[1]
    shutil.copytree(root / "config", tmp_path / "config")
    shutil.copytree(root / "templates", tmp_path / "templates")
    wf = Workflow(settings)
    run = wf.init_run("test_fixture")
    wf.scan(run)
    wf.select(run, ["C1"])
    ingest(settings, package("delivery-topic"))
    stamp = datetime.now(timezone.utc).isoformat()
    with wf.db.connect() as conn:
        conn.execute("""INSERT INTO research_reviews(id,run_id,candidate_id,topic_id,input_hash,
                     decision,confidence,research_allowed,data_json,report_path,human_decision,created_at)
                     VALUES(?,?,'C1','delivery-topic','hash','proceed','high',1,'{}','fixture','proceed',?)""", (run + ':review', run, stamp))
    source = tmp_path / "draft.md"
    source.write_text(valid_text(), encoding="utf-8")
    return settings, wf, run, source


def test_short_slogan_draft_fails_deterministic_gate(draft_context):
    settings, wf, run, source = draft_context
    source.write_text("# 测试\n中关村支部：李智\n一、现状\n现状\n二、问题和分析\n问题\n三、政策建议\n高度重视", encoding="utf-8")
    check = check_draft(settings, "delivery-topic", source)
    assert not check["ok"]
    assert any("字数" in error for error in check["errors"])
    assert any("空泛" in warning for warning in check["warnings"])
    assert any("分项" in error for error in check["errors"])


@pytest.mark.parametrize("kind", ["facts", "mechanism_red_team", "problem_suggestion_mapping", "style_structure"])
def test_each_review_is_required(draft_context, kind):
    settings, wf, run, source = draft_context
    record = review_record(settings, run, "delivery-topic", source)
    payload = json.loads(record.read_text())
    del payload["reviews"][kind]
    record.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=kind):
        register_review(settings, run, "delivery-topic", source, record)


def test_evidence_change_invalidates_review_and_export(draft_context):
    settings, wf, run, source = draft_context
    record_valid_review(settings, run, "delivery-topic", source)
    before = require_review(settings, run, "delivery-topic", source)
    with wf.db.connect() as conn:
        conn.execute("UPDATE claims SET uncertainty_reason='新增限制条件' WHERE topic_id='delivery-topic'")
    with pytest.raises(ValueError, match="版本已变化"):
        require_review(settings, run, "delivery-topic", source)
    record_valid_review(settings, run, "delivery-topic", source)
    assert require_review(settings, run, "delivery-topic", source)["evidence_sha256"] != before["evidence_sha256"]


@pytest.mark.parametrize('value', [None, 0, 12.5, -1, float('nan'), True])
def test_human_minutes_are_optional_and_never_invented(draft_context, value):
    settings, wf, run, source = draft_context
    path = review_record(settings, run, 'delivery-topic', source)
    payload = json.loads(path.read_text()); payload['human_effort'] = {'review_minutes': value, 'major_fact_changes': None}
    path.write_text(json.dumps(payload))
    if value is None or (type(value) in (int, float) and value >= 0):
        key = register_review(settings, run, 'delivery-topic', source, path)
        with wf.db.connect() as conn:
            record = json.loads(conn.execute('SELECT result_json FROM tasks WHERE id=?', (key,)).fetchone()[0])
        assert record['human_effort']['review_minutes'] == value
        assert record['human_effort']['major_fact_changes'] is None
    else:
        with pytest.raises(ValueError, match='人工投入'):
            register_review(settings, run, 'delivery-topic', source, path)


def test_tampered_docx_cannot_be_approved_and_revisions_count_once(draft_context):
    settings, wf, run, source = draft_context
    record_valid_review(settings, run, "delivery-topic", source)
    output = wf.draft(run, "delivery-topic", source)
    with output.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="导出后发生变化"):
        wf.approve("delivery-topic")
    wf.draft(run, "delivery-topic", source)
    with wf.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone()[0] == 1
    assert wf.approve("delivery-topic").is_file()
    assert production_funnel(settings)["totals"]["calendar_draft_count"] == 0  # fixture隔离


def test_calendar_counts_delivery_not_old_discovery_week(draft_context):
    settings, wf, run, source = draft_context
    record_valid_review(settings, run, "delivery-topic", source)
    wf.draft(run, "delivery-topic", source)
    with wf.db.connect() as conn:
        old = (datetime.now(timezone.utc) - timedelta(weeks=8)).isoformat()
        conn.execute("UPDATE runs SET created_at=? WHERE id=?", (old, run))
        conn.execute("UPDATE run_context SET mode='live' WHERE run_id=?", (run,))
    report = production_funnel(settings)
    assert report["totals"]["live_runs"] == 0
    assert report["weeks"][-1]["calendar_draft_count"] == 1
    assert report["weeks"][-1]["minimum_target_met"]


def test_direct_index_extracts_unique_links_with_dates_and_no_body_fetch(tmp_path):
    settings = settings_at(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/sources.toml").write_text('sources=[]', encoding="utf-8")
    collector = SourceCollector(tmp_path, settings.raw)
    day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    source = dict(id="direct", name="直属目录", level=1, region="全国", type="html_index",
                  url="https://example.gov.cn/news/", item_url_prefix="https://example.gov.cn/news/", max_items=2)
    html = f'<li><a href="./one.html" title="民生政策测试">标题缩略</a><a href="./one.html">重复</a><span>{day}</span></li>'
    with patch("sqmy.collector.urlopen", side_effect=AssertionError("no body fetch")):
        items = collector._items_from_payloads([dict(source=source, xml=html)])
    assert len(items) == 1
    assert items[0].title == "民生政策测试"
    assert items[0].summary == ""
    with pytest.raises(ValueError, match="目录未解析"):
        listing_to_rss('<li><a href="/news/one.html">无日期</a></li>', source)


def test_tracking_order_dedup_preserves_content_parameters():
    assert canonical_url("https://x.test/view?id=1&utm_source=x&page=2") == canonical_url("https://x.test/view?page=2&id=1")
    assert canonical_url("https://x.test/view?id=1") != canonical_url("https://x.test/view?id=2")


def test_phrase_mention_is_a_review_warning_not_automatic_rejection(draft_context):
    settings, wf, run, source = draft_context
    source.write_text(source.read_text() + "不得以高度重视替代具体纠错流程。", encoding="utf-8")
    check = check_draft(settings, "delivery-topic", source)
    assert check["ok"]
    assert check["warnings"]


def test_approved_file_change_blocks_submission_registration(draft_context):
    settings, wf, run, source = draft_context
    record_valid_review(settings, run, "delivery-topic", source)
    wf.draft(run, "delivery-topic", source)
    final = wf.approve("delivery-topic")
    with final.open("ab") as stream:
        stream.write(b"changed-after-approval")
    with pytest.raises(ValueError, match="人工审核版本不一致"):
        wf.mark_submitted("delivery-topic", "2026-09-06", "测试接收方")
    with wf.db.connect() as conn:
        assert conn.execute("SELECT actually_submitted FROM topics").fetchone()[0] == 0


@pytest.mark.parametrize("reason", [None, [], "   "])
def test_empty_review_reason_is_not_a_content_review(draft_context, reason):
    settings, wf, run, source = draft_context
    record = review_record(settings, run, "delivery-topic", source)
    payload = json.loads(record.read_text())
    payload["reviews"]["facts"]["reason"] = reason
    record.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="facts"):
        register_review(settings, run, "delivery-topic", source, record)


def test_four_closed_weeks_require_four_existing_verified_deliveries(draft_context):
    settings, wf, run, source = draft_context
    record_valid_review(settings, run, "delivery-topic", source)
    output = wf.draft(run, "delivery-topic", source)
    reference = datetime.now(timezone.utc)
    with wf.db.connect() as conn:
        conn.execute("UPDATE run_context SET mode='live' WHERE run_id=?", (run,))
        event = dict(conn.execute("SELECT * FROM delivery_events").fetchone())
        for i in range(1, 5):
            topic = f"closed-week-{i}"
            conn.execute("INSERT INTO topics(id,title,created_at,run_id) VALUES(?,?,?,?)",
                         (topic, topic, reference.isoformat(), run))
            conn.execute("INSERT INTO delivery_events VALUES(?,?,?,?,?,?,?)", (
                topic, run, (reference - timedelta(weeks=i)).isoformat(), str(output),
                event["output_sha256"], event["evidence_sha256"], event["review_id"]))
    report = production_funnel(settings, reference=reference)
    assert report["stable_minimum_output"]
    assert not report["stable_stretch_output"]
    output.unlink()
    report = production_funnel(settings, reference=reference)
    assert not report["stable_minimum_output"]
    assert report["totals"]["calendar_draft_count"] == 0


@pytest.mark.parametrize('suffix', [
    'https://example.org/source', 'HTTP://example.org', 'www.example.org',
    'example.org/source', 'person@example.org', 'file:///private/source',
    '[来源](https://example.org)', '[来源](../source.md)', '[来源][ref]',
    '[ref]: ../source.md', 'mailto:someone@example.org',
    '来源example.org。', '来源example.org，', '来源example.org）',
    '来源 example.xyz/source', '来源example.dev。', '来源example.technology；',
])
def test_formal_body_links_are_blocked(draft_context, suffix):
    settings, wf, run, source = draft_context
    source.write_text(valid_text() + '\n' + suffix, encoding='utf-8')
    check = check_draft(settings, 'delivery-topic', source)
    assert any('URL或Markdown链接' in error for error in check['errors'])


def test_soft_target_warns_without_rejecting_hard_range_and_old_config(draft_context):
    settings, wf, run, source = draft_context
    check = check_draft(settings, 'delivery-topic', source)
    count = check['body_chars']
    assert check['ok']
    settings.raw['writing_quality']['target_max_chars'] = count - 1
    assert check_draft(settings, 'delivery-topic', source)['ok']
    assert any('通常正文目标' in x for x in check_draft(settings, 'delivery-topic', source)['warnings'])
    settings.raw['writing_quality'].pop('target_min_chars')
    settings.raw['writing_quality'].pop('target_max_chars')
    old = check_draft(settings, 'delivery-topic', source)
    assert old['ok']
    assert not any('通常正文目标' in x for x in old['warnings'])
    source.write_text(valid_text() + '字' * 1900)
    assert any('字数' in x for x in check_draft(settings, 'delivery-topic', source)['errors'])


@pytest.mark.parametrize('kind', ['hyperlink', 'field', 'simple_field', 'visible_url', 'relationship',
                                'domain_chinese_punctuation', 'domain_other_tld', 'split_domain'])
def test_docx_hidden_or_visible_links_blocked_without_network(tmp_path, kind):
    from sqmy.delivery import require_link_free_docx
    import zipfile
    ns='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    elements = {
        'hyperlink': '<w:hyperlink><w:r><w:t>来源</w:t></w:r></w:hyperlink>',
        'field': '<w:r><w:instrText> HYPER</w:instrText></w:r><w:r><w:instrText>LINK "https://example.org" </w:instrText></w:r>',
        'simple_field': '<w:fldSimple w:instr="HYPERLINK &quot;https://example.org&quot;"/>',
        'visible_url': '<w:r><w:t>https://example.org</w:t></w:r>',
        'relationship': '<w:r><w:t>正常文本</w:t></w:r>',
        'domain_chinese_punctuation': '<w:r><w:t>来源example.org。</w:t></w:r>',
        'domain_other_tld': '<w:r><w:t>example.xyz/source</w:t></w:r>',
        'split_domain': '<w:r><w:t>来源example.</w:t></w:r><w:r><w:t>xyz。</w:t></w:r>',
    }
    path=tmp_path/'links.docx'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('word/document.xml', f'<w:document xmlns:w="{ns}"><w:body><w:p>{elements[kind]}</w:p></w:body></w:document>')
        if kind == 'relationship':
            z.writestr('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.org" TargetMode="External"/></Relationships>')
    with pytest.raises(ValueError, match='链接'):
        require_link_free_docx(path)


def test_plain_docx_and_evidence_link_context_remain_allowed(draft_context):
    from sqmy.delivery import require_link_free_docx
    settings, wf, run, source = draft_context
    record_valid_review(settings, run, 'delivery-topic', source)
    output = wf.draft(run, 'delivery-topic', source)
    require_link_free_docx(output)
    # fixture证据来源自带URL；正式正文检查不删除或拒绝内部来源链接。
    assert wf.approve('delivery-topic').exists()


def test_versions_decimals_and_document_numbers_are_not_domains(draft_context):
    from sqmy.delivery import require_link_free_docx
    settings, wf, run, source = draft_context
    source.write_text(valid_text() + '\n版本v3.0，费率3.5%，依据文号〔2026〕12号。', encoding='utf-8')
    assert check_draft(settings, 'delivery-topic', source)['ok']
    record_valid_review(settings, run, 'delivery-topic', source)
    require_link_free_docx(wf.draft(run, 'delivery-topic', source))


def test_linked_template_cannot_create_delivery_event(draft_context):
    from docx import Document
    settings, wf, run, source = draft_context
    record_valid_review(settings, run, 'delivery-topic', source)
    template = settings.root / settings.section('document')['template_path']
    doc=Document(template);doc.add_paragraph('https://example.org');doc.save(template)
    with pytest.raises(ValueError, match='URL或链接'):
        wf.draft(run, 'delivery-topic', source)
    with wf.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM delivery_events').fetchone()[0] == 0


@pytest.mark.parametrize('size,major,ok,warn', [
    (1200, False, True, False), (1500, False, True, False),
    (1501, False, True, True), (1800, False, True, True),
    (1801, False, False, True), (2000, True, True, False),
    (2501, True, False, False),
])
def test_length_target_hard_limit_and_major_exception(draft_context, size, major, ok, warn):
    settings, wf, run, source = draft_context
    text = '# 正式测试稿\n中关村支部：李智\n一、现状\n现状\n二、问题和分析\n（一）问题\n（二）问题\n（三）问题\n三、政策建议\n（一）建议\n（二）建议\n（三）建议\n'
    text = text.replace('\n（', '\n\n（')
    source.write_text(text)
    base = check_draft(settings, 'delivery-topic', source, major=major)['body_chars']
    source.write_text(text + '字' * (size-base))
    result = check_draft(settings, 'delivery-topic', source, major=major)
    assert result['body_chars'] == size
    assert result['ok'] is ok
    assert any('通常正文目标' in x for x in result['warnings']) is warn
