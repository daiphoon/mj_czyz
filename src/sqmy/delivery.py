"""确定性送审检查与版本绑定，不把格式校验冒充内容质量审查。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .db import Database, now
from .document import parse_submission_markdown
from .evidence import assess_topic, evidence_contract
from .research_gate import require_pre_research_approval

REVIEW_KINDS = ("facts", "mechanism_red_team", "problem_suggestion_mapping", "style_structure")


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def evidence_fingerprint(db, topic_id):
    contract = evidence_contract(db, topic_id)
    detail_column = ",cs.evidence_detail_json" if contract == "evidence_v2" else ""
    with db.connect() as conn:
        claims = [dict(row) for row in conn.execute("SELECT * FROM claims WHERE topic_id=? ORDER BY id", (topic_id,))]
        links = [dict(row) for row in conn.execute(
            f"""SELECT cs.claim_id,cs.source_id,cs.evidence_role,cs.origin_group,
               cs.source_level,cs.primary_source,cs.notes{detail_column},su.metadata_json,su.needs_review FROM claim_sources cs
               JOIN claims c ON c.id=cs.claim_id
               LEFT JOIN source_usages su ON su.topic_id=c.topic_id AND su.source_id=cs.source_id
               WHERE c.topic_id=? ORDER BY cs.claim_id,cs.source_id""", (topic_id,))]
    for row in claims:
        row.pop("created_at", None)
    value = [claims, links] if contract == "evidence_v1" else {"contract": contract, "claims": claims, "links": links}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_draft(settings, topic_id, source: Path, *, major=False):
    db = Database(settings.database_path)
    db.initialize()
    cfg = settings.section("writing_quality")
    text = source.read_text(encoding="utf-8")
    errors = []
    headings = ("一、现状", "二、问题和分析", "三、政策建议")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    observed = [line.lstrip("#").strip() for line in lines if line.lstrip("#").strip() in headings]
    if observed != list(headings):
        errors.append("三级结构顺序错误、重复或缺失")
    if len(lines) < 2 or lines[1] != settings.section("project")["signature"]:
        errors.append("署名缺失、位置或内容不正确")
    title, sections = parse_submission_markdown(source)
    if sum(line.startswith("# ") for line in lines) != 1:
        errors.append("必须只有一个正式标题")
    if len(lines) < 3 or lines[2].lstrip("#").strip() != headings[0]:
        errors.append("标题和署名后应直接进入现状，不设其他前置内容")
    if any(line.startswith("#") and line != lines[0] and line.lstrip("#").strip() not in headings for line in lines):
        errors.append("存在固定结构以外的标题")
    body = "".join(p for values in sections.values() for p in values)
    count = len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", body))
    low, high = (cfg["major_min_chars"], cfg["major_max_chars"]) if major else (cfg["min_chars"], cfg["max_chars"])
    if not low <= count <= high:
        errors.append(f"正文有效字数{count}，要求{low}—{high}（不计标题、署名、空白和标点）")
    markers = {}
    for heading in headings[1:]:
        found = re.findall(r"(?:^|\n)\s*（([一二三四五六七八九十]+)）", "\n".join(sections[heading]))
        markers[heading] = found
        if not cfg["min_items"] <= len(found) <= cfg["max_items"] or len(found) != len(set(found)):
            errors.append(f"{heading}分项数量须为{cfg['min_items']}—{cfg['max_items']}且不得重复")
    if markers[headings[1]] != markers[headings[2]]:
        errors.append("问题与建议编号不对应")
    warnings = [f"需结合上下文审查是否空泛：{p}" for p in cfg["vague_phrases"] if p in body]
    if re.search(r"摘要[：:]|关键词[：:]|参考文献|\[\^|^\s*\|", text, re.M):
        errors.append("正文含禁止的摘要、关键词、参考文献、脚注或表格")
    gate = assess_topic(settings, topic_id)
    if not gate["draft_allowed"]:
        errors.append("证据闸门未通过")
    from .research_brief import brief_status
    brief = brief_status(settings, topic_id)
    errors.extend(brief["errors"])
    from .semantic_review import current_review
    semantic = current_review(settings, topic_id)
    if semantic:
        warnings.append(f"存在语义影子意见：{semantic['status']}；事实审查应回到原文裁决，模型意见不改变证据闸门。")
    return dict(topic_id=topic_id, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                evidence_contract=evidence_contract(db, topic_id),
                research_brief_sha256=brief["sha256"],
                semantic_review_sha256=semantic['review_sha256'] if semantic else None,
                evidence_sha256=evidence_fingerprint(db, topic_id), major=major, body_chars=count,
                title=title, problem_ids=markers[headings[1]],
                critical_claim_ids=[r["claim_id"] for r in gate["claims"] if r["importance"] == "critical"],
                ok=not errors, errors=errors, warnings=warnings,
                note="仅核验确定性规则；措辞提示不等于空泛，事实、机制与副作用仍须有依据的内容审查")


def _review_key(topic_id, source_hash, evidence_hash, brief_hash=None):
    return hashlib.sha256((topic_id + source_hash + evidence_hash + (brief_hash or "")).encode()).hexdigest()


def register_review(settings, run_id, topic_id, source: Path, record: Path):
    payload = json.loads(record.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("major", False), bool):
        raise ValueError("送审审查记录格式无效")
    check = check_draft(settings, topic_id, source, major=payload.get("major", False))
    errors = list(check["errors"])
    for key in ("topic_id", "source_sha256", "evidence_sha256"):
        if payload.get(key) != check[key]:
            errors.append(f"审查记录与当前版本不一致：{key}")
    if payload.get("evidence_contract", "evidence_v1") != check["evidence_contract"]:
        errors.append("审查记录与当前证据契约不一致：evidence_contract")
    if payload.get("research_brief_sha256") != check["research_brief_sha256"]:
        errors.append("审查记录与研究简报版本不一致：research_brief_sha256")
    if check["research_brief_sha256"]:
        from .research_brief import latest_brief
        brief = latest_brief(Database(settings.database_path), topic_id, run_id)
        mapping = payload.get("problem_option_map")
        selected = set(brief.get("selected_option_ids", [])) if brief else set()
        if (not isinstance(mapping, dict) or set(mapping) != set(check["problem_ids"])
            or any(not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in selected for r in refs)
                   for refs in mapping.values())):
            errors.append("问题—建议审查须逐项映射到研究简报中选定的方案")
    reviews = payload.get("reviews", {})
    for kind in REVIEW_KINDS:
        item = reviews.get(kind, {}) if isinstance(reviews, dict) else {}
        if not isinstance(item, dict) or item.get("status") != "passed" or not _nonempty(item.get("reason")) or not _nonempty(item.get("reviewer")):
            errors.append(f"缺少有效内容审查：{kind}")
    if payload.get("claim_ids") != check["critical_claim_ids"]:
        errors.append("事实审查未覆盖全部核心主张")
    if payload.get("problem_ids") != check["problem_ids"]:
        errors.append("问题—建议审查未覆盖正文分项")
    if check["major"] and not _nonempty(payload.get("major_reason")):
        errors.append("重大事项扩展字数须说明理由")
    try:
        reviewed = datetime.fromisoformat(payload["reviewed_at"])
        if reviewed.tzinfo is None or reviewed > datetime.now(timezone.utc):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        errors.append("审查日期须含时区且不得在未来")
    effort = payload.get("human_effort")
    if effort is not None:
        import math
        if not isinstance(effort, dict) or set(effort) - {"review_minutes", "major_fact_changes", "major_mechanism_changes"}:
            errors.append("人工投入字段无效")
        else:
            for name in ("review_minutes", "major_fact_changes", "major_mechanism_changes"):
                value = effort.get(name)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value) or value < 0
                                          or (name != "review_minutes" and not isinstance(value, int))):
                    errors.append(f"人工投入 {name} 必须为非负数（修改次数为整数），未知留 null")
    if errors:
        raise ValueError("送审检查未通过：" + "；".join(errors))
    db = Database(settings.database_path)
    key = _review_key(topic_id, check["source_sha256"], check["evidence_sha256"], check["research_brief_sha256"])
    task_id = f"{run_id}:draft_quality_review:{key[:16]}"
    with db.connect() as conn:
        candidate = conn.execute("SELECT candidate_id FROM research_reviews WHERE run_id=? AND topic_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (run_id, topic_id)).fetchone()
        if not candidate:
            raise ValueError("未找到本题人工深研放行记录")
    require_pre_research_approval(settings, run_id, candidate["candidate_id"], topic_id)
    with db.connect() as conn:
        conn.execute("""INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at)
                     VALUES(?,?,'draft_quality_review',?,'completed',?,?)
                     ON CONFLICT(run_id,kind,input_hash) DO UPDATE SET result_json=excluded.result_json,updated_at=excluded.updated_at""",
                     (task_id, run_id, key, json.dumps(payload, ensure_ascii=False), now()))
    return task_id


def require_review(settings, run_id, topic_id, source):
    db = Database(settings.database_path)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence_hash = evidence_fingerprint(db, topic_id)
    from .research_brief import brief_status
    brief = brief_status(settings, topic_id, run_id)
    if brief["errors"]:
        raise ValueError("送审检查未通过：" + "；".join(brief["errors"]))
    key = _review_key(topic_id, source_hash, evidence_hash, brief["sha256"])
    with db.connect() as conn:
        row = conn.execute("SELECT id,result_json FROM tasks WHERE run_id=? AND kind='draft_quality_review' AND input_hash=? AND status='completed'", (run_id, key)).fetchone()
    if not row:
        raise ValueError("送审检查未完成或版本已变化，请先运行 draft-check 和 draft-review")
    check = check_draft(settings, topic_id, source, major=json.loads(row["result_json"]).get("major", False))
    if not check["ok"]:
        raise ValueError("送审检查未通过：" + "；".join(check["errors"]))
    return dict(check, review_id=row["id"])


def verify_export(settings, topic, path):
    db = Database(settings.database_path)
    with db.connect() as conn:
        rows = conn.execute("SELECT result_json FROM tasks WHERE run_id=? AND kind='draft_export' AND status='completed' ORDER BY updated_at DESC", (topic["run_id"],)).fetchall()
    for row in rows:
        result = json.loads(row["result_json"])
        if result.get("topic_id") != topic["id"]:
            continue
        if not result.get("delivery_verified"):
            break
        check = require_review(settings, topic["run_id"], topic["id"], Path(topic["draft_source_path"]))
        if check["source_sha256"] != result.get("source_sha256") or check["evidence_sha256"] != result.get("evidence_sha256"):
            break
        if check["research_brief_sha256"] != result.get("research_brief_sha256"):
            break
        if hashlib.sha256(path.read_bytes()).hexdigest() != result.get("output_sha256"):
            raise ValueError("送审文件在导出后发生变化，须重新导出并审核")
        return
    raise ValueError("缺少绑定当前版本的送审检查，不得记录人工通过")


def verify_approved_integrity(settings, topic):
    """已通过的旧稿不追溯改状态；新流程稿件外发前须与审核版本完全一致。"""
    db = Database(settings.database_path)
    with db.connect() as conn:
        rows = conn.execute("SELECT result_json FROM tasks WHERE run_id=? AND kind='draft_export' AND status='completed'", (topic["run_id"],)).fetchall()
    for row in rows:
        result = json.loads(row["result_json"])
        if result.get("topic_id") == topic["id"] and result.get("delivery_verified"):
            if hashlib.sha256(Path(topic["final_path"]).read_bytes()).hexdigest() != result.get("output_sha256"):
                raise ValueError("待报送文件与人工审核版本不一致，须重新确认修改版本")
            return
