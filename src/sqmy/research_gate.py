from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .budget import record_stage_usage
from .cadence import require_source_freshness
from .config import Settings
from .db import Database, now
from .models import Phase, TaskStatus
from .novelty import record_pre_research_feedback
from .source_trace import pre_research_trace
from .problem_mechanism import validate_problem, render_problem


DECISIONS = {"proceed", "reframe", "stop"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
DECISION_REASONS = {
    "original_gap_supported",
    "policy_covered",
    "insufficient_evidence",
    "no_local_authority",
    "low_public_value",
    "high_side_effect_risk",
    "mechanism_not_viable",
    "reframe_required",
    "other",
    "fact_contradicted",
    "source_unread",
    "tool_failure",
    "claim_too_broad",
    "causal_gap",
}
SOURCE_ROLES = {
    "official_policy",
    "official_data",
    "court_case",
    "media_investigation",
    "academic_research",
    "pain_signal",
    "comparative_case",
    "other",
}
MECHANISM_FIELDS = {
    "proposal",
    "actor",
    "target",
    "trigger",
    "information",
    "cost_bearer",
    "beneficiary",
    "expected_behavior",
    "evasion_risk",
    "cost_transfer_risk",
    "verification",
    "correction",
    "exit_condition",
    "lower_cost_alternative",
}
SCENARIO_FIELDS = {"baseline", "most_likely", "adverse"}


def _canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_date(value: Any) -> bool:
    if not _is_nonempty(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _require_nonempty_list(payload: dict[str, Any], key: str, errors: list[str], *, required=True) -> list[Any]:
    value = payload.get(key, [] if not required else None)
    if not isinstance(value, list) or (required and not value):
        errors.append(f"{key} 必须是非空列表")
        return []
    return value


def validate_pre_research_payload(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    contract = payload.get("research_contract", "research_v1")
    v3 = contract == "research_v3"
    stopped = v3 and payload.get("decision") == "stop"
    if contract not in {"research_v1", "research_v3"}:
        errors.append("不支持的 research_contract")
    for key in ("run_id", "candidate_id", "topic_id", "working_title", "confidence_reason"):
        if not _is_nonempty(payload.get(key)):
            errors.append(f"{key} 不能为空")

    decision = payload.get("decision")
    if decision not in DECISIONS:
        errors.append("decision 只能是 proceed、reframe 或 stop")
    decision_reason = payload.get("decision_reason")
    if decision_reason is None:
        (errors if v3 else warnings).append("缺少 decision_reason；本次预研只能生成未分类反馈，后续决策单应补齐")
    elif decision_reason not in DECISION_REASONS:
        errors.append("decision_reason 不是支持的标准原因")
    confidence = payload.get("confidence")
    if confidence not in CONFIDENCE_LEVELS:
        errors.append("confidence 只能是 low、medium 或 high")

    sources = _require_nonempty_list(payload, "sources", errors, required=not stopped)
    source_keys: set[str] = set()
    origin_groups: set[str] = set()
    has_level_one = False
    for index, source in enumerate(sources, 1):
        if not isinstance(source, dict):
            errors.append(f"sources[{index}] 必须是对象")
            continue
        key = source.get("key")
        if not _is_nonempty(key):
            errors.append(f"sources[{index}].key 不能为空")
        elif key in source_keys:
            errors.append(f"来源 key 重复：{key}")
        else:
            source_keys.add(key)
        for field in ("url", "publisher", "origin_group"):
            if not _is_nonempty(source.get(field)):
                errors.append(f"来源 {key or index} 缺少 {field}")
        if _is_nonempty(source.get("origin_group")):
            origin_groups.add(source["origin_group"].strip())
        if source.get("source_level") not in {1, 2, 3}:
            errors.append(f"来源 {key or index} 的 source_level 必须是 1、2 或 3")
        has_level_one = has_level_one or source.get("source_level") == 1
        if source.get("source_role") not in SOURCE_ROLES:
            errors.append(f"来源 {key or index} 的 source_role 未分类或无效")
        if not _valid_date(source.get("published_at")):
            warnings.append(f"来源 {key or index} 缺少有效发布时间")
        if not _valid_date(source.get("checked_at")):
            errors.append(f"来源 {key or index} 缺少有效核验时间")

    referenced_sections = (
        "verified_facts",
        "evidence_based_inferences",
        "counterevidence",
    )
    for section in referenced_sections:
        items = _require_nonempty_list(payload, section, errors,
                                       required=not stopped and not (v3 and section == "evidence_based_inferences"))
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict) or not _is_nonempty(item.get("statement")):
                errors.append(f"{section}[{index}] 缺少 statement")
                continue
            refs = item.get("source_keys", [])
            if not isinstance(refs, list) or not refs:
                errors.append(f"{section}[{index}] 缺少 source_keys")
            else:
                unknown = sorted(set(refs) - source_keys)
                if unknown:
                    errors.append(f"{section}[{index}] 引用了未知来源：{', '.join(unknown)}")
            if section == "evidence_based_inferences":
                for field in ("basis", "uncertainty", "falsifier"):
                    if not _is_nonempty(item.get(field)):
                        errors.append(f"{section}[{index}] 缺少 {field}")
            if section == "counterevidence" and not _is_nonempty(item.get("implication")):
                errors.append(f"{section}[{index}] 缺少 implication")

    hypotheses = _require_nonempty_list(payload, "unverified_hypotheses", errors, required=not v3)
    for index, item in enumerate(hypotheses, 1):
        if not isinstance(item, dict):
            errors.append(f"unverified_hypotheses[{index}] 必须是对象")
            continue
        for field in ("statement", "verification_plan", "discard_if"):
            if not _is_nonempty(item.get(field)):
                errors.append(f"unverified_hypotheses[{index}] 缺少 {field}")

    judgments = _require_nonempty_list(payload, "analyst_judgments", errors, required=not v3)
    for index, item in enumerate(judgments, 1):
        if not isinstance(item, dict):
            errors.append(f"analyst_judgments[{index}] 必须是对象")
            continue
        for field in ("statement", "rationale"):
            if not _is_nonempty(item.get(field)):
                errors.append(f"analyst_judgments[{index}] 缺少 {field}")

    alternatives = _require_nonempty_list(payload, "alternative_explanations", errors, required=not v3)
    for index, item in enumerate(alternatives, 1):
        if not isinstance(item, dict) or not _is_nonempty(item.get("statement")) or not _is_nonempty(item.get("test")):
            errors.append(f"alternative_explanations[{index}] 必须包含 statement 和 test")

    unknowns = payload.get("critical_unknowns", [])
    if not isinstance(unknowns, list):
        errors.append("critical_unknowns 必须是列表")
        unknowns = []
    for index, item in enumerate(unknowns, 1):
        if not isinstance(item, dict):
            errors.append(f"critical_unknowns[{index}] 必须是对象")
            continue
        for field in ("question", "resolution_plan"):
            if not _is_nonempty(item.get(field)):
                errors.append(f"critical_unknowns[{index}] 缺少 {field}")
        if not isinstance(item.get("blocking"), bool):
            errors.append(f"critical_unknowns[{index}].blocking 必须是布尔值")
    blocking_unknowns = [
        item.get("question", f"unknown-{index}")
        for index, item in enumerate(unknowns, 1)
        if isinstance(item, dict) and item.get("blocking") is True
    ]

    for key in ("discard_conditions", "research_questions"):
        values = _require_nonempty_list(payload, key, errors, required=not (stopped and key == "research_questions"))
        if any(not _is_nonempty(item) for item in values):
            errors.append(f"{key} 只能包含非空字符串")

    authority = payload.get("authority")
    if stopped and authority is None:
        pass
    elif not isinstance(authority, dict):
        errors.append("authority 必须是对象")
    else:
        for field in ("actor", "power", "boundary"):
            if not _is_nonempty(authority.get(field)):
                errors.append(f"authority 缺少 {field}")

    budget = payload.get("budget")
    if not isinstance(budget, dict):
        errors.append("budget 必须是对象")
    else:
        token_limit = budget.get("token_limit")
        configured_limit = int(settings.section("budget")["pre_research_tokens"])
        if not isinstance(token_limit, int) or token_limit <= 0:
            errors.append("budget.token_limit 必须是正整数")
        elif token_limit > configured_limit:
            errors.append(
                f"预研预算 {token_limit} 超过配置上限 {configured_limit}，必须先调整配置并记录理由"
            )
        for field in ("reason", "expected_benefit"):
            if not _is_nonempty(budget.get(field)):
                errors.append(f"budget 缺少 {field}")

    mechanisms = _require_nonempty_list(payload, "mechanism_cards", errors, required=not v3)
    for index, mechanism in enumerate(mechanisms, 1):
        if not isinstance(mechanism, dict):
            errors.append(f"mechanism_cards[{index}] 必须是对象")
            continue
        for field in sorted(MECHANISM_FIELDS):
            if not _is_nonempty(mechanism.get(field)):
                errors.append(f"mechanism_cards[{index}] 缺少 {field}")
        scenarios = mechanism.get("scenarios")
        if not isinstance(scenarios, dict):
            errors.append(f"mechanism_cards[{index}].scenarios 必须是对象")
        else:
            for field in sorted(SCENARIO_FIELDS):
                if not _is_nonempty(scenarios.get(field)):
                    errors.append(f"mechanism_cards[{index}].scenarios 缺少 {field}")

    if v3:
        if stopped:
            for field in ("stop_summary", "reopen_condition", "effort_note"):
                if not _is_nonempty(payload.get(field)):
                    errors.append(f"停止记录缺少 {field}")
        else:
            claim_ids = [item.get("id") for section in referenced_sections for item in payload.get(section, []) if isinstance(item, dict)]
            if any(not _is_nonempty(i) for i in claim_ids) or len(set(i for i in claim_ids if isinstance(i, str))) != len(claim_ids):
                errors.append("新版预研的事实、推断与反证 id 必须非空且唯一")
            errors.extend(validate_problem(payload.get("problem_mechanism"), {i for i in claim_ids if isinstance(i, str)}))
            feasibility = payload.get("initial_feasibility", {})
            for field in ("authority", "major_risks", "lower_cost_alternative"):
                if not isinstance(feasibility, dict) or not _is_nonempty(feasibility.get(field)):
                    errors.append(f"初步可行性缺少 {field}")

    # 记录可用于停止原因反馈，不代表证据充分或允许进入深研。
    record_valid = not errors
    diagnostics = pre_research_trace(payload) if record_valid else {
        "reason_codes": ["RECORD_INVALID"], "evidence_checks": [], "open_questions": [],
    }
    for check in diagnostics["evidence_checks"]:
        if check["code"] != "TRACE_PRESENT":
            errors.append(f"{check['claim_ref']} 来源 {check['source_key']}：{check['reason']}")
    if any(c.get("legacy") for c in diagnostics["evidence_checks"]):
        warnings.append("旧来源保留摘录但未结构化记录抓取状态；兼容读取，不补称全文核验")
    if decision_reason == "fact_contradicted":
        errors.append("已记录核心事实被可靠证据否定，须纠正命题并重新核验，不能直接放行")
    if decision in {"proceed", "reframe"}:
        if len(source_keys) < 2 or len(origin_groups) < 2:
            errors.append("继续研究至少需要两个独立原始信息链")
        if not has_level_one:
            errors.append("继续研究至少需要一个一级来源")
    if blocking_unknowns:
        errors.append("仍有阻断性关键未知：" + "；".join(blocking_unknowns))

    research_allowed = decision in {"proceed", "reframe"} and not errors
    diagnostics["disposition"] = (
        "record_invalid" if not record_valid else
        "rejected_cut" if "FACT_FALSE" in diagnostics["reason_codes"] else
        "tech_blocked" if "TOOL_FAILURE" in diagnostics["reason_codes"] else
        "human_review" if research_allowed else
        "unclassified" if "UNCLASSIFIED_DECISION" in diagnostics["reason_codes"] else
        "rejected_cut" if diagnostics["reason_codes"] and set(diagnostics["reason_codes"]) <= {"FACT_FALSE", "POLICY_DUPLICATE"} else
        "needs_evidence"
    )
    return {
        "valid": not errors,
        "record_valid": record_valid,
        "research_allowed": research_allowed,
        "errors": errors,
        "warnings": warnings,
        "blocking_unknowns": blocking_unknowns,
        "diagnostics": diagnostics,
    }


def _render_items(items: list[Any], *, statement_key: str = "statement") -> list[str]:
    lines = []
    for item in items:
        if isinstance(item, dict):
            statement = item.get(statement_key) or json.dumps(item, ensure_ascii=False)
            lines.append(f"- {statement}")
        else:
            lines.append(f"- {item}")
    return lines or ["- 无"]


def _render_detailed_items(items: list[Any], fields: tuple[tuple[str, str], ...]) -> list[str]:
    lines = []
    for item in items:
        if not isinstance(item, dict):
            lines.append(f"- {item}")
            continue
        prefix = f"[{item['id']}] " if item.get('id') else ""
        lines.append(f"- {prefix}{item.get('statement', '')}")
        for field, label in fields:
            value = item.get(field)
            if value:
                if isinstance(value, list):
                    value = "、".join(str(part) for part in value)
                lines.append(f"  - {label}：{value}")
    return lines or ["- 无"]


def _render_report(payload: dict[str, Any], gate: dict[str, Any]) -> str:
    authority = payload.get("authority") or {}
    budget = payload.get("budget", {})
    unknown_lines = []
    for item in payload.get("critical_unknowns", []):
        marker = "阻断" if item.get("blocking") else "待深研"
        unknown_lines.extend(
            [
                f"- [{marker}] {item.get('question', '')}",
                f"  - 解决路径：{item.get('resolution_plan', '')}",
            ]
        )
    mechanism_lines = []
    for index, item in enumerate(payload.get("mechanism_cards", []), 1):
        mechanism_lines.extend(
            [
                f"### 机制 {index}：{item.get('proposal', '')}",
                "",
                f"- 行动主体：{item.get('actor', '')}",
                f"- 对象与触发：{item.get('target', '')}；{item.get('trigger', '')}",
                f"- 信息条件：{item.get('information', '')}",
                f"- 成本与收益：{item.get('cost_bearer', '')}；{item.get('beneficiary', '')}",
                f"- 预期行为：{item.get('expected_behavior', '')}",
                f"- 规避与转嫁：{item.get('evasion_risk', '')}；{item.get('cost_transfer_risk', '')}",
                f"- 核验与纠错：{item.get('verification', '')}；{item.get('correction', '')}",
                f"- 退出与低成本替代：{item.get('exit_condition', '')}；{item.get('lower_cost_alternative', '')}",
                f"- 基线情形：{item.get('scenarios', {}).get('baseline', '')}",
                f"- 最可能情形：{item.get('scenarios', {}).get('most_likely', '')}",
                f"- 不利情形：{item.get('scenarios', {}).get('adverse', '')}",
                "",
            ]
        )
    source_lines = [
        f"- [{item.get('key', '')}] L{item.get('source_level', '')} "
        f"{item.get('publisher', '')}｜{item.get('source_role', '')}｜"
        f"原始链 {item.get('origin_group', '')}｜{item.get('url', '')}\n"
        f"  - 获取状态：{item.get('fetch_status', 'legacy_unclassified')}；定位：{item.get('locator', '未结构化记录')}\n"
        f"  - 必要摘录：{item.get('excerpt', '未提供')}\n"
        f"  - 适用限制：{item.get('limitation', item.get('data_scope', '未结构化记录'))}\n"
        f"  - 不能推出：{item.get('cannot_infer', '需结合主张核验')}"
        for item in payload.get("sources", [])
    ]
    lines = [
        f"# 有限预研决策单：{payload.get('working_title', '')}",
        "",
        f"- 运行：{payload.get('run_id', '')}",
        f"- 候选：{payload.get('candidate_id', '')}",
        f"- 题目ID：{payload.get('topic_id', '')}",
        f"- 分析决定：{payload.get('decision', '')}",
        *([f"- 研究契约：{payload['research_contract']}"] if payload.get('research_contract') else []),
        f"- 决定原因：{payload.get('decision_reason', 'unclassified')}",
        f"- 置信度：{payload.get('confidence', '')}（{payload.get('confidence_reason', '')}）",
        f"- 允许进入人工深研确认：{'是' if gate['research_allowed'] else '否'}",
        "",
        "## 闸门结果",
        "",
        *(_render_items(gate["errors"]) if gate["errors"] else ["- 无阻断项"]),
        *[f"- 提醒：{item}" for item in gate["warnings"]],
        f"- 处置分类：{gate.get('diagnostics', {}).get('disposition', '未分类')}",
        f"- 原因码：{', '.join(gate.get('diagnostics', {}).get('reason_codes', [])) or '无'}",
        "- 以上是记录及引用检查，不是自动内容审查结论。",
        "",
        "## 核心来源",
        "",
        *(source_lines or ["- 无"]),
        "",
        "## 已验证事实",
        "",
        *_render_detailed_items(payload.get("verified_facts", []), (("source_keys", "来源"),)),
        "",
        "## 基于证据的推断",
        "",
        *_render_detailed_items(
            payload.get("evidence_based_inferences", []),
            (("basis", "依据"), ("uncertainty", "不确定性"), ("falsifier", "证伪条件"), ("source_keys", "来源")),
        ),
        "",
        "## 尚未验证的假设",
        "",
        *_render_detailed_items(
            payload.get("unverified_hypotheses", []),
            (("verification_plan", "核验路径"), ("discard_if", "放弃条件")),
        ),
        "",
        "## 分析判断",
        "",
        *_render_detailed_items(payload.get("analyst_judgments", []), (("rationale", "理由"),)),
        "",
        "## 最强反证与其他解释",
        "",
        *_render_detailed_items(
            payload.get("counterevidence", []),
            (("implication", "影响"), ("source_keys", "来源")),
        ),
        *_render_detailed_items(
            payload.get("alternative_explanations", []),
            (("test", "核验方法"),),
        ),
        "",
        "## 关键未知",
        "",
        *(unknown_lines or ["- 无"]),
        "",
        "## 权限边界",
        "",
        f"- 行动主体：{authority.get('actor', '')}",
        f"- 可用权限：{authority.get('power', '')}",
        f"- 权限边界：{authority.get('boundary', '')}",
        "",
        "## 深研问题和放弃条件",
        "",
        *_render_items(payload.get("research_questions", [])),
        "",
        "### 放弃或改写条件",
        "",
        *_render_items(payload.get("discard_conditions", [])),
        "",
        "## 阶段预算",
        "",
        f"- Token上限：{budget.get('token_limit', '')}",
        f"- 提高或使用理由：{budget.get('reason', '')}",
        f"- 预期收益：{budget.get('expected_benefit', '')}",
        "",
        "## 机制压力测试",
        "",
        *(mechanism_lines or ["- 无"]),
        *render_problem(payload.get("problem_mechanism")),
        *(["## 停止依据与重开条件", "", f"- {payload.get('stop_summary', '')}",
           f"- 重开条件：{payload.get('reopen_condition', '')}", f"- 实际投入：{payload.get('effort_note', '')}", ""]
          if payload.get("research_contract") == "research_v3" and payload.get("decision") == "stop" else []),
        *(["## 初步可行性", "", *[f"- {k}：{v}" for k, v in payload['initial_feasibility'].items()], ""]
          if isinstance(payload.get('initial_feasibility'), dict) else []),
        "> 本决策单是内部研究记录，不得作为正式报送正文。只有人工记录 proceed 后才能进入深研。",
    ]
    return "\n".join(lines) + "\n"


def check_pre_research(
    settings: Settings,
    run_id: str,
    candidate_id: str,
    brief_path: Path,
) -> dict[str, Any]:
    payload = json.loads(brief_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("预研决策单顶层必须是JSON对象")
    if payload.get("run_id") != run_id or payload.get("candidate_id") != candidate_id:
        raise ValueError("预研决策单中的运行ID或候选ID与命令不一致")

    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        candidate = conn.execute(
            """SELECT c.title,c.selected,c.data_json FROM candidates c
               WHERE c.run_id=? AND c.id=?""",
            (run_id, f"{run_id}:{candidate_id}"),
        ).fetchone()
        run = conn.execute(
            "SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    if run is None:
        raise ValueError(f"未找到运行：{run_id}")
    if candidate is None:
        raise ValueError(f"候选题不存在：{candidate_id}")
    if not candidate["selected"]:
        raise ValueError("候选题尚未人工选择")
    candidate_contract = (json.loads(candidate['data_json']).get('eligibility') or {}).get('contract')
    if candidate_contract == 'candidate_shadow_v1' and payload.get('research_contract') != 'research_v3':
        raise ValueError('新版候选须使用 research_v3 分阶段预研契约；旧运行仍按原版本恢复')
    require_source_freshness(settings, db, run_id)

    gate = validate_pre_research_payload(settings, payload)
    input_hash = _canonical_hash(payload)
    with db.connect() as conn:
        latest = conn.execute(
            "SELECT input_hash,data_json FROM research_reviews WHERE run_id=? AND candidate_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (run_id, candidate_id),
        ).fetchone()
        previous = conn.execute(
            "SELECT 1 FROM research_reviews WHERE run_id=? AND candidate_id=? AND input_hash=?",
            (run_id, candidate_id, input_hash),
        ).fetchone()
    if latest and json.loads(latest["data_json"]).get("research_contract") == "research_v3" and payload.get("research_contract") != "research_v3":
        raise ValueError("新版研究记录不能降级为旧契约")
    if previous and latest["input_hash"] != input_hash:
        raise ValueError("不能用旧版本决策单恢复放行；请基于最新证据形成新版本并重新确认")
    review_id = f"{run_id}:{candidate_id}:{input_hash[:12]}"
    report = (
        settings.root
        / "outputs/review/pre_research"
        / run_id
        / f"{candidate_id}_decision.md"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_report(payload, gate)
    temporary = report.with_suffix(report.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, report)

    record = payload | {
        "gate": gate,
        "input_hash": input_hash,
        "report_path": str(report),
        "review_id": review_id,
    }
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO research_reviews(
                 id,run_id,candidate_id,topic_id,input_hash,decision,confidence,
                 research_allowed,data_json,report_path,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id,candidate_id,input_hash) DO UPDATE SET
                 decision=excluded.decision,confidence=excluded.confidence,
                 research_allowed=excluded.research_allowed,data_json=excluded.data_json,
                 report_path=excluded.report_path""",
            (
                review_id,
                run_id,
                candidate_id,
                payload.get("topic_id", ""),
                input_hash,
                payload.get("decision", "stop"),
                payload.get("confidence", "low"),
                int(gate["research_allowed"]),
                json.dumps(record, ensure_ascii=False),
                str(report),
                now(),
            ),
        )
    # 先持久化预研记录，再回写派生的制度新意反馈；中断后重试仍保持幂等。
    feedback = record_pre_research_feedback(settings, run_id, candidate_id, payload, gate)
    record["novelty_feedback"] = feedback
    with db.connect() as conn:
        conn.execute(
            "UPDATE research_reviews SET data_json=? WHERE id=?",
            (json.dumps(record, ensure_ascii=False), review_id),
        )
        persisted = conn.execute(
            "SELECT human_decision,human_note,reviewed_at FROM research_reviews WHERE id=?",
            (review_id,),
        ).fetchone()

    checkpoint = json.loads(run["checkpoint_json"] or "{}")
    checkpoint["pre_research"] = {
        "candidate_id": candidate_id,
        "topic_id": payload.get("topic_id"),
        "review_id": review_id,
        "decision": payload.get("decision"),
        "research_allowed": gate["research_allowed"],
        "report_path": str(report),
        "novelty_feedback": feedback,
    }
    human_decision = persisted["human_decision"] if persisted else None
    if human_decision:
        checkpoint["pre_research"].update(
            {
                "human_decision": human_decision,
                "human_note": persisted["human_note"],
                "reviewed_at": persisted["reviewed_at"],
            }
        )
    if gate["research_allowed"] and human_decision == "proceed":
        status = TaskStatus.PENDING
        checkpoint["next"] = "按决策单的研究问题开展深研；完成后运行 sqmy evidence-import PATH"
    elif human_decision == "stop":
        status = TaskStatus.NEEDS_REVIEW
        checkpoint["next"] = f"sqmy skip-run {run_id} --reason REASON，或重新选择候选"
    elif payload.get("decision") == "stop" and gate["record_valid"]:
        status = TaskStatus.NEEDS_REVIEW
        checkpoint["next"] = "有限预研已停止；等待用户决定，不得仅为放行而修改结论、自动转题或深研"
    else:
        status = TaskStatus.NEEDS_REVIEW
        checkpoint["next"] = (
            f"sqmy pre-research-review {run_id} {candidate_id} --decision proceed --note REVIEW_NOTE"
            if gate["research_allowed"]
            else f"修订 {brief_path} 后重新运行 sqmy pre-research-check"
        )
    db.checkpoint(
        run_id,
        phase=Phase.RESEARCH,
        status=status,
        data=checkpoint,
    )
    record_stage_usage(
        db,
        run_id=run_id,
        topic_id=str(payload.get("topic_id") or f"{run_id}:{candidate_id}"),
        stage="pre_research",
        token_used=int(payload.get("budget", {}).get("token_limit", 0)),
        input_hash=input_hash,
        provider="codex_subscription",
        model=settings.section("model")["codex_model"],
        note="有限预研决策单落库时按其声明上限保守记账；不是Plus官方Token统计。",
    )
    return record


def review_pre_research(
    settings: Settings,
    run_id: str,
    candidate_id: str,
    *,
    decision: str,
    note: str,
) -> str:
    if decision not in {"proceed", "stop"}:
        raise ValueError("人工决定只能是 proceed 或 stop")
    if not note.strip():
        raise ValueError("必须记录人工复核理由")
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        review = conn.execute(
            """SELECT * FROM research_reviews
               WHERE run_id=? AND candidate_id=?
               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (run_id, candidate_id),
        ).fetchone()
        run = conn.execute(
            "SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    if run is None:
        raise ValueError(f"未找到运行：{run_id}")
    if review is None:
        raise ValueError("尚未形成有限预研决策单")
    if decision == "proceed" and not review["research_allowed"]:
        raise ValueError("预研存在阻断项，不能人工放行深研；请先修订决策单")

    reviewed_at = now()
    with db.connect() as conn:
        conn.execute(
            """UPDATE research_reviews SET human_decision=?,human_note=?,reviewed_at=?
               WHERE id=?""",
            (decision, note.strip(), reviewed_at, review["id"]),
        )
    checkpoint = json.loads(run["checkpoint_json"] or "{}")
    checkpoint.setdefault("pre_research", {}).update(
        {
            "review_id": review["id"],
            "human_decision": decision,
            "human_note": note.strip(),
            "reviewed_at": reviewed_at,
        }
    )
    if decision == "proceed":
        next_action = "按决策单的研究问题开展深研；完成后运行 sqmy evidence-import PATH"
        status = TaskStatus.PENDING
    else:
        next_action = f"sqmy skip-run {run_id} --reason REASON，或重新选择候选"
        status = TaskStatus.NEEDS_REVIEW
    checkpoint["next"] = next_action
    db.checkpoint(run_id, phase=Phase.RESEARCH, status=status, data=checkpoint)
    return next_action


def require_pre_research_approval(
    settings: Settings,
    run_id: str,
    candidate_id: str,
    topic_id: str,
) -> None:
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        review = conn.execute(
            """SELECT topic_id,research_allowed,human_decision FROM research_reviews
               WHERE run_id=? AND candidate_id=?
               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (run_id, candidate_id),
        ).fetchone()
    if review is None:
        raise ValueError("预研闸门未通过：尚未形成有限预研决策单")
    if review["topic_id"] != topic_id:
        raise ValueError("预研闸门未通过：尚未形成该题目最新版本的人工放行")
    if not review["research_allowed"]:
        raise ValueError("预研闸门未通过：决策单仍有阻断项")
    if review["human_decision"] != "proceed":
        raise ValueError("预研闸门未通过：尚未人工确认进入深研")
