from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .budget import record_stage_usage
from .config import Settings
from .db import Database, now
from .source_trace import read_issue


@dataclass(frozen=True)
class ClaimAssessment:
    claim_id: str
    claim_text: str
    importance: str
    epistemic_status: str
    confidence: str
    status: str
    independent_origins: int
    supporting_sources: int
    contradicting_sources: int
    metadata_issues: list[str]
    reasons: list[str]


ALLOWED_EPISTEMIC_STATUSES = {
    "verified_fact",
    "evidence_based_inference",
    "unverified_hypothesis",
    "analyst_judgment",
}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
STATISTIC_SCOPE_FIELDS = {"time_period", "region", "population", "unit", "definition"}
SOURCE_ROLES = {
    "official_policy", "official_data", "court_case", "media_investigation",
    "academic_research", "pain_signal", "comparative_case", "other",
}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _json_value(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _parse_datetime(value: str | None) -> datetime | None:
    if not _nonempty(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _unresolved_conflicts(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [value]
    if not isinstance(parsed, list):
        return [parsed]
    return [item for item in parsed if not isinstance(item, dict) or not item.get("resolved")]


def import_evidence_package(settings: Settings, path: Path) -> str:
    """Import a small, reviewable claim/source package idempotently."""
    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or not _nonempty(payload.get("topic_id")):
        raise ValueError("证据包必须包含非空topic_id")
    for key in ("sources", "claims"):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"证据包{key}必须是列表")
    for key, field in (("sources", "key"), ("claims", "id")):
        identifiers = [item.get(field) if isinstance(item, dict) else None for item in payload[key]]
        if any(not _nonempty(value) for value in identifiers) or len(set(identifiers)) != len(identifiers):
            raise ValueError(f"{key}中的{field}必须非空且不得重复")
    topic_id = payload["topic_id"]
    db = Database(settings.database_path)
    db.initialize()
    source_ids: dict[str, int] = {}
    with db.connect() as conn:
        for source in payload["sources"]:
            fetched_at = source.get("fetched_at", now())
            conn.execute(
                """INSERT INTO sources(
                     topic_id,source_name,page_title,url,publisher,published_at,fetched_at,
                     excerpt,data_scope,used_at,content_hash,source_role,checked_at,effective_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url,content_hash) DO NOTHING""",
                (
                    topic_id, source["source_name"], source["page_title"], source["url"],
                    source.get("publisher"), source.get("published_at"), fetched_at,
                    source.get("excerpt"), _json_value(source.get("data_scope")), source.get("used_at"),
                    source.get("content_hash", source["url"]),
                    source.get("source_role", "unclassified"),
                    source.get("checked_at"), source.get("effective_at"),
                ),
            )
            row = conn.execute(
                "SELECT id FROM sources WHERE url=? AND content_hash=?",
                (source["url"], source.get("content_hash", source["url"])),
            ).fetchone()
            source_ids[source["key"]] = int(row[0])
            metadata = dict(source, fetched_at=fetched_at)
            conn.execute(
                """INSERT INTO source_usages(topic_id,source_id,metadata_json,needs_review)
                   VALUES(?,?,?,0) ON CONFLICT(topic_id,source_id) DO UPDATE SET
                   metadata_json=excluded.metadata_json,needs_review=0""",
                (topic_id, row[0], json.dumps(metadata, ensure_ascii=False)),
            )

        for claim in payload["claims"]:
            existing = conn.execute(
                "SELECT id FROM claims WHERE topic_id=? AND local_id=?", (topic_id, claim["id"])
            ).fetchone()
            stored_id = existing["id"] if existing else claim["id"]
            owner = conn.execute("SELECT topic_id,local_id FROM claims WHERE id=?", (stored_id,)).fetchone()
            if not existing and owner is not None:
                identity = json.dumps([topic_id, claim["id"]], ensure_ascii=False)
                stored_id = "scoped:" + hashlib.sha256(identity.encode()).hexdigest()
                owner = conn.execute("SELECT topic_id,local_id FROM claims WHERE id=?", (stored_id,)).fetchone()
            if owner and (owner["topic_id"], owner["local_id"]) != (topic_id, claim["id"]):
                raise ValueError("主张内部编号冲突，导入已取消")
            conn.execute(
                """INSERT INTO claims(
                     id,local_id,topic_id,claim_text,claim_type,importance,novelty_required,
                     policy_coverage_status,epistemic_status,confidence,
                     uncertainty_reason,falsifier,as_of_date,scope_json,reasoning,
                     conflicts_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     claim_text=excluded.claim_text, claim_type=excluded.claim_type,
                     importance=excluded.importance, novelty_required=excluded.novelty_required,
                     policy_coverage_status=excluded.policy_coverage_status,
                     epistemic_status=excluded.epistemic_status,confidence=excluded.confidence,
                     uncertainty_reason=excluded.uncertainty_reason,
                     falsifier=excluded.falsifier,as_of_date=excluded.as_of_date,
                     scope_json=excluded.scope_json,reasoning=excluded.reasoning,
                     conflicts_json=excluded.conflicts_json""",
                (
                    stored_id, claim["id"], topic_id, claim["claim_text"], claim["claim_type"],
                    claim["importance"], int(claim.get("novelty_required", False)),
                    claim.get("policy_coverage_status", "unchecked"),
                    claim.get("epistemic_status", "unclassified"),
                    claim.get("confidence", "unrated"), claim.get("uncertainty_reason"),
                    claim.get("falsifier"), claim.get("as_of_date"),
                    _json_value(claim.get("scope")), claim.get("reasoning"),
                    _json_value(claim.get("conflicts")), now(),
                ),
            )
            conn.execute("DELETE FROM claim_sources WHERE claim_id=?", (stored_id,))
            for link in claim.get("sources", []):
                conn.execute(
                    """INSERT INTO claim_sources(
                         claim_id,source_id,evidence_role,origin_group,source_level,
                         primary_source,notes
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        stored_id, source_ids[link["source"]], link["role"],
                        link["origin_group"], link["source_level"],
                        int(link.get("primary_source", False)), link.get("notes"),
                    ),
                )
    with db.connect() as conn:
        review = conn.execute(
            """SELECT run_id FROM research_reviews rr
               WHERE topic_id=? AND research_allowed=1 AND human_decision='proceed'
               AND rr.rowid=(SELECT newer.rowid FROM research_reviews newer
                   WHERE newer.run_id=rr.run_id AND newer.candidate_id=rr.candidate_id
                   ORDER BY newer.created_at DESC,newer.rowid DESC LIMIT 1)
               ORDER BY reviewed_at DESC,created_at DESC,rowid DESC LIMIT 1""",
            (topic_id,),
        ).fetchone()
    if review is not None:
        record_stage_usage(
            db,
            run_id=review["run_id"],
            topic_id=topic_id,
            stage="deep_research",
            token_used=int(settings.section("budget")["deep_research_tokens"]),
            input_hash=hashlib.sha256(raw_bytes).hexdigest(),
            provider="codex_subscription",
            model=settings.section("model")["codex_model"],
            note="结构化证据包导入时按深研阶段上限保守记账；不是Plus官方Token统计。",
        )
    return topic_id


def assess_topic(settings: Settings, topic_id: str) -> dict[str, Any]:
    rules = settings.section("evidence")
    db = Database(settings.database_path)
    db.initialize()
    assessments: list[ClaimAssessment] = []
    with db.connect() as conn:
        claims = conn.execute("SELECT * FROM claims WHERE topic_id=? ORDER BY id", (topic_id,)).fetchall()
        for claim in claims:
            links = conn.execute(
                """SELECT cs.*,u.metadata_json,u.needs_review
                   FROM claim_sources cs LEFT JOIN source_usages u
                     ON u.source_id=cs.source_id AND u.topic_id=?
                   WHERE cs.claim_id=?""",
                (topic_id, claim["id"]),
            ).fetchall()
            supporting = [row for row in links if row["evidence_role"] == "supports"]
            contradicting = [row for row in links if row["evidence_role"] == "contradicts"
                             and not read_issue(_parse_json_object(row["metadata_json"]))]
            # Third-tier material remains a clue, not an independent verification chain.
            qualified = [row for row in supporting if row["source_level"] in {1, 2}
                         and _nonempty(row["origin_group"])
                         and not read_issue(_parse_json_object(row["metadata_json"]))
                         and _parse_json_object(row["metadata_json"]).get("source_role") != "pain_signal"]
            if rules["same_origin_reprints_count_once"]:
                origins = {row["origin_group"].strip() for row in qualified}
            else:
                origins = {str(row["source_id"]) for row in qualified}
            official_primary = any(row["source_level"] == 1 and row["primary_source"] for row in qualified)
            reasons: list[str] = []
            metadata_issues: list[str] = []

            if contradicting:
                status = "conflicted"
                reasons.append("存在直接反证，必须先解决冲突")
            elif not qualified:
                status = "unverified"
                reasons.append("没有合格的一、二级来源支持该主张；三级来源仅作线索")
            elif len(origins) >= rules["min_independent_origins"]:
                status = "cross_verified"
                reasons.append(f"由 {len(origins)} 个独立信息源链支持")
            elif rules["official_primary_source_can_stand_alone"] and official_primary:
                status = "single_authoritative"
                reasons.append("存在单一正式一级原始来源")
            else:
                status = "single_source"
                reasons.append("多个页面仍属同一原始发布链，不算交叉验证")

            if claim["novelty_required"]:
                coverage = claim["policy_coverage_status"]
                if coverage == "covered":
                    status = "rejected"
                    reasons.append("现有政策或机制已覆盖该缺口")
                elif coverage != "gap_supported":
                    status = "needs_review"
                    reasons.append("制度新意主张尚未完成政策覆盖核查")

            epistemic = claim["epistemic_status"]
            if claim["importance"] not in {"critical", "supporting"}:
                metadata_issues.append("主张重要性分类无效")
            for row in links:
                if row["evidence_role"] in {"supports", "contradicts"}:
                    issue = read_issue(_parse_json_object(row["metadata_json"]))
                    if issue:
                        metadata_issues.append(f"来源 {row['source_id']} [{issue[0]}] {issue[1]}")
                if row["source_level"] not in {1, 2, 3} or row["evidence_role"] not in {"supports", "contradicts", "context"}:
                    metadata_issues.append("来源等级或支持关系无效")
                if not _nonempty(row["origin_group"]):
                    metadata_issues.append("来源缺少独立信息链标识")
            if epistemic not in ALLOWED_EPISTEMIC_STATUSES:
                metadata_issues.append("认识状态未分类或取值无效")
            elif epistemic in {"unverified_hypothesis", "analyst_judgment"}:
                metadata_issues.append("假设或分析判断不得直接进入正式正文")

            if claim["claim_type"] == "statistic":
                scope = _parse_json_object(claim["scope_json"])
                missing_scope = sorted(
                    field for field in STATISTIC_SCOPE_FIELDS if not _nonempty(scope.get(field))
                )
                if missing_scope:
                    metadata_issues.append("统计口径缺少：" + "、".join(missing_scope))
            if claim["importance"] == "critical":
                if claim["confidence"] not in ALLOWED_CONFIDENCE:
                    metadata_issues.append("核心主张未标注置信度")
                if not _nonempty(claim["uncertainty_reason"]):
                    metadata_issues.append("核心主张未说明不确定性来源")
                if not _nonempty(claim["falsifier"]):
                    metadata_issues.append("核心主张未记录可证伪条件")
                as_of = _parse_datetime(claim["as_of_date"])
                if as_of is None or as_of > datetime.now(timezone.utc):
                    metadata_issues.append("核心主张缺少有效的截至日期")
                if epistemic == "evidence_based_inference" and not _nonempty(claim["reasoning"]):
                    metadata_issues.append("证据推断未记录推理依据")
                max_age = int(rules["critical_source_max_age_days"])
                stale_before = datetime.now(timezone.utc) - timedelta(days=max_age)
                for row in supporting:
                    metadata = _parse_json_object(row["metadata_json"])
                    role = metadata.get("source_role")
                    if not _nonempty(role) or role not in SOURCE_ROLES:
                        metadata_issues.append(f"来源 {row['source_id']} 未标注用途")
                    if row["needs_review"]:
                        metadata_issues.append(f"来源 {row['source_id']} 的历史跨题共享记录需重新导入核验")
                    checked_at = _parse_datetime(metadata.get("checked_at"))
                    if checked_at is None:
                        metadata_issues.append(f"来源 {row['source_id']} 缺少有效核验时间")
                    elif checked_at < stale_before:
                        metadata_issues.append(f"来源 {row['source_id']} 已超过 {max_age} 天未核验")
                    elif checked_at > datetime.now(timezone.utc):
                        metadata_issues.append(f"来源 {row['source_id']} 核验时间位于未来")

            if _unresolved_conflicts(claim["conflicts_json"]):
                status = "conflicted"
                metadata_issues.append("主张记录中仍有未解决的来源冲突")
            elif metadata_issues and status not in {"conflicted", "rejected", "unverified", "single_source"}:
                status = "needs_review"
            reasons.extend(metadata_issues)

            assessments.append(ClaimAssessment(
                claim_id=claim["local_id"] or claim["id"], claim_text=claim["claim_text"], importance=claim["importance"],
                epistemic_status=epistemic, confidence=claim["confidence"],
                status=status, independent_origins=len(origins), supporting_sources=len(supporting),
                contradicting_sources=len(contradicting), metadata_issues=metadata_issues,
                reasons=reasons,
            ))

    blocking = set(rules["blocking_statuses"])
    blockers = [
        a.claim_id for a in assessments
        if (a.status in blocking and (a.importance == "critical" or a.status != "single_source"))
        or bool(a.metadata_issues)
    ]
    gate_errors: list[str] = []
    if not assessments:
        gate_errors.append("未录入任何主张")
    elif not any(item.importance == "critical" for item in assessments):
        gate_errors.append("未录入核心主张")
    return {
        "topic_id": topic_id,
        "draft_allowed": not blockers and not gate_errors,
        "blocking_claims": blockers,
        "gate_errors": gate_errors,
        "claims": [asdict(item) for item in assessments],
    }
