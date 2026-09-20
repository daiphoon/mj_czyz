"""深研的单一写作输入；存储于 tasks，文件是可重建的阅读副本。"""
import hashlib
import json
import os

from .budget import record_stage_usage
from .db import Database, now
from .evidence import assess_topic, evidence_contract
from .problem_mechanism import nonempty, render_problem, validate_problem
from .research_gate import MECHANISM_FIELDS, SCENARIO_FIELDS, require_pre_research_approval


CONTRACT = "research_brief_v1"
OPTION_FIELDS = {"authority_basis", "rights_impact", "implementation_capacity", "outcome_indicator", "warning_indicator"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def latest_pre_research(db, topic_id, run_id=None):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_reviews WHERE topic_id=?" + (" AND run_id=?" if run_id else "")
            + " ORDER BY created_at DESC,rowid DESC LIMIT 1", (topic_id, run_id) if run_id else (topic_id,),
        ).fetchone()
    return dict(row) if row else None


def latest_brief(db, topic_id, run_id=None):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE kind='research_brief' AND json_extract(result_json,'$.topic_id')=?"
            + (" AND run_id=?" if run_id else "") + " ORDER BY rowid DESC LIMIT 1",
            (topic_id, run_id) if run_id else (topic_id,),
        ).fetchone()
    return json.loads(row["result_json"]) if row else None


def validate_brief(payload, claim_ids):
    errors = []
    if payload.get("brief_contract") != CONTRACT:
        errors.append("研究简报契约无效")
    writing = payload.get("outcome") == "write"
    if payload.get("outcome") not in {"write", "no_draft"}:
        errors.append("研究结果须为 write/no_draft")
    for key in ("conclusion", "comparison_reason"):
        if not nonempty(payload.get(key)):
            errors.append(f"研究简报缺少 {key}")
    errors.extend(validate_problem(payload.get("problem_mechanism"), claim_ids, deep=writing, allow_no_working=not writing))
    unknowns = payload.get("remaining_unknowns")
    if not isinstance(unknowns, list):
        errors.append("remaining_unknowns 必须为列表")
    else:
        for u in unknowns:
            if not isinstance(u, dict) or not nonempty(u.get("question")) or not isinstance(u.get("blocking"), bool):
                errors.append("剩余未知格式无效")
            elif writing and u["blocking"]:
                errors.append("仍有阻断性未知，不能形成写作输入")
    if not writing:
        if not nonempty(payload.get("reopen_condition")):
            errors.append("不成稿须说明重开条件")
        return errors
    options = payload.get("options")
    if not isinstance(options, list) or not options:
        return errors + ["缺少基线与最低必要干预比较"]
    problem = payload.get("problem_mechanism") or {}
    hypotheses = problem.get("hypotheses", []) if isinstance(problem, dict) and isinstance(problem.get('hypotheses'), list) else []
    supported = {h["id"] for h in hypotheses if isinstance(h, dict) and nonempty(h.get("id")) and h.get("status") == "supported"}
    ids, kinds = set(), []
    for option in options:
        if not isinstance(option, dict):
            errors.append("方案必须为对象")
            continue
        key, kind = option.get("id"), option.get("kind")
        if not nonempty(key) or key in ids:
            errors.append("方案 id 非空且唯一")
        else:
            ids.add(key)
        kinds.append(kind)
        if kind not in {"baseline", "minimum", "stronger"}:
            errors.append("方案 kind 无效")
        for field in ("proposal", "expected_effect", "cost", "risk"):
            if not nonempty(option.get(field)):
                errors.append(f"方案 {key} 缺少 {field}")
        refs = option.get("target_mechanism_ids")
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in supported for r in refs):
            errors.append(f"方案 {key} 未映射到已有证据支持的问题机制")
        if kind != "baseline":
            for field in sorted(MECHANISM_FIELDS | OPTION_FIELDS):
                if not nonempty(option.get(field)):
                    errors.append(f"方案 {key} 缺少 {field}")
            scenarios = option.get("scenarios")
            if not isinstance(scenarios, dict) or any(not nonempty(scenarios.get(k)) for k in SCENARIO_FIELDS):
                errors.append(f"方案 {key} 缺少三种情形压力测试")
    if kinds.count("baseline") != 1 or kinds.count("minimum") != 1:
        errors.append("须各有一个 baseline 和 minimum 方案；较强干预按需比较")
    selected = payload.get("selected_option_ids")
    if not isinstance(selected, list) or not selected or any(not isinstance(k, str) or k not in ids for k in selected):
        errors.append("缺少有效 selected_option_ids")
    return errors


def brief_status(settings, topic_id, run_id=None):
    from .delivery import evidence_fingerprint
    db = Database(settings.database_path)
    pre = latest_pre_research(db, topic_id, run_id)
    brief = latest_brief(db, topic_id, run_id)
    required = bool(brief or (pre and json.loads(pre["data_json"]).get("research_contract") == "research_v3"))
    errors = []
    if required and not brief:
        errors.append("新版研究须先登记 research-brief，形成绑定证据的写作输入")
    if brief:
        if brief["evidence_sha256"] != evidence_fingerprint(db, topic_id):
            errors.append("研究简报绑定的证据已变化")
        if not pre or brief["pre_research_review_id"] != pre["id"]:
            errors.append("研究简报对应的预研批准版本已变化")
        elif not pre["research_allowed"] or pre["human_decision"] != "proceed":
            errors.append("研究简报缺少有效人工深研许可")
        if brief["outcome"] != "write":
            errors.append("最新深研结论为不成稿")
    return {"required": required, "sha256": brief["brief_sha256"] if brief else None, "errors": errors}


def register_brief(settings, run_id, topic_id, path):
    from .delivery import evidence_fingerprint
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("run_id") != run_id or payload.get("topic_id") != topic_id:
        raise ValueError("研究简报运行或题目不匹配")
    db = Database(settings.database_path); db.initialize()
    pre = latest_pre_research(db, topic_id, run_id)
    if not pre or payload.get("pre_research_review_id") != pre["id"] or payload.get("candidate_id") != pre["candidate_id"]:
        raise ValueError("研究简报须绑定最新预研批准版本")
    require_pre_research_approval(settings, run_id, pre["candidate_id"], topic_id)
    if payload.get("evidence_sha256") != evidence_fingerprint(db, topic_id):
        raise ValueError("研究简报证据版本不匹配")
    if evidence_contract(db, topic_id) != "evidence_v2":
        raise ValueError("新版深研须使用 evidence_v2 逐主张证据")
    gate = assess_topic(settings, topic_id)
    errors = validate_brief(payload, {c["claim_id"] for c in gate["claims"]})
    if payload.get("outcome") == "write" and not gate["draft_allowed"]:
        errors.append("证据闸门未通过")
    if errors:
        raise ValueError("研究简报检查未通过：" + "；".join(errors))
    # 输出字段不能由导入者注入；文件与任务以同一内容哈希命名。
    for key in ("brief_sha256", "json_path", "markdown_path", "registered_at"):
        payload.pop(key, None)
    key = _hash(payload)
    with db.connect() as conn:
        previous = conn.execute("SELECT 1 FROM tasks WHERE run_id=? AND kind='research_brief' AND input_hash=?", (run_id, key)).fetchone()
    latest = latest_brief(db, topic_id, run_id)
    if previous and latest["brief_sha256"] != key:
        raise ValueError("不能用旧版本研究简报恢复通过；须依据最新研究修订")
    base = settings.root / "outputs/review/research_briefs" / run_id / key
    result = payload | {"brief_sha256": key, "json_path": str(base.with_suffix('.json')),
                        "markdown_path": str(base.with_suffix('.md'))}
    with db.connect() as conn:
        conn.execute("""INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at)
                     VALUES(?,?,'research_brief',?,'completed',?,?) ON CONFLICT(run_id,kind,input_hash) DO NOTHING""",
                     (f"{run_id}:research_brief:{key}", run_id, key, json.dumps(result, ensure_ascii=False), now()))
    record_stage_usage(db, run_id=run_id, topic_id=topic_id, stage="deep_research",
                       token_used=int(settings.section("budget")["deep_research_tokens"]), input_hash=key,
                       provider="codex_subscription", model=settings.section("model")["codex_model"],
                       note="深研简报耐久边界按阶段上限幂等估算；与证据导入共用阶段，不是官方Token。")
    # 即使写文件时中断，重复登记同一内容也可重建；不改变 task 版本顺序。
    base.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 深研简报与写作输入", "", f"- 题目：{topic_id}", f"- 材料版本：{key}",
             f"- 结果：{payload['outcome']}", f"- 结论：{payload['conclusion']}",
             f"- 方案比较：{payload['comparison_reason']}", "", *render_problem(payload["problem_mechanism"]),
             "## 完整结构化输入", "", "以下是同一任务内容的阅读副本；修改后须重新登记。字段检查不证明解释或方案正确。", "",
             "```json", json.dumps(payload, ensure_ascii=False, indent=2), "```", ""]
    for target, text in ((base.with_suffix('.json'), json.dumps(result, ensure_ascii=False, indent=2)),
                         (base.with_suffix('.md'), "\n".join(lines))):
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8"); os.replace(temp, target)
    return result
