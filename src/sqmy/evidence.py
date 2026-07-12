from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, now


@dataclass(frozen=True)
class ClaimAssessment:
    claim_id: str
    claim_text: str
    importance: str
    status: str
    independent_origins: int
    supporting_sources: int
    contradicting_sources: int
    reasons: list[str]


def import_evidence_package(settings: Settings, path: Path) -> str:
    """Import a small, reviewable claim/source package idempotently."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    topic_id = payload["topic_id"]
    db = Database(settings.database_path)
    db.initialize()
    source_ids: dict[str, int] = {}
    with db.connect() as conn:
        for source in payload["sources"]:
            conn.execute(
                """INSERT OR IGNORE INTO sources(
                     topic_id,source_name,page_title,url,publisher,published_at,fetched_at,
                     excerpt,data_scope,used_at,content_hash
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    topic_id, source["source_name"], source["page_title"], source["url"],
                    source.get("publisher"), source.get("published_at"), source.get("fetched_at", now()),
                    source.get("excerpt"), source.get("data_scope"), source.get("used_at"),
                    source.get("content_hash", source["url"]),
                ),
            )
            row = conn.execute(
                "SELECT id FROM sources WHERE url=? AND content_hash=?",
                (source["url"], source.get("content_hash", source["url"])),
            ).fetchone()
            source_ids[source["key"]] = int(row[0])

        for claim in payload["claims"]:
            conn.execute(
                """INSERT INTO claims(
                     id,topic_id,claim_text,claim_type,importance,novelty_required,
                     policy_coverage_status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     claim_text=excluded.claim_text, claim_type=excluded.claim_type,
                     importance=excluded.importance, novelty_required=excluded.novelty_required,
                     policy_coverage_status=excluded.policy_coverage_status""",
                (
                    claim["id"], topic_id, claim["claim_text"], claim["claim_type"],
                    claim["importance"], int(claim.get("novelty_required", False)),
                    claim.get("policy_coverage_status", "unchecked"), now(),
                ),
            )
            conn.execute("DELETE FROM claim_sources WHERE claim_id=?", (claim["id"],))
            for link in claim.get("sources", []):
                conn.execute(
                    """INSERT INTO claim_sources(
                         claim_id,source_id,evidence_role,origin_group,source_level,
                         primary_source,notes
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        claim["id"], source_ids[link["source"]], link["role"],
                        link["origin_group"], link["source_level"],
                        int(link.get("primary_source", False)), link.get("notes"),
                    ),
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
                "SELECT * FROM claim_sources WHERE claim_id=?", (claim["id"],)
            ).fetchall()
            supporting = [row for row in links if row["evidence_role"] == "supports"]
            contradicting = [row for row in links if row["evidence_role"] == "contradicts"]
            if rules["same_origin_reprints_count_once"]:
                origins = {row["origin_group"] for row in supporting}
            else:
                origins = {str(row["source_id"]) for row in supporting}
            official_primary = any(row["source_level"] == 1 and row["primary_source"] for row in supporting)
            reasons: list[str] = []

            if contradicting:
                status = "conflicted"
                reasons.append("存在直接反证，必须先解决冲突")
            elif not supporting:
                status = "unverified"
                reasons.append("没有支持该主张的来源")
            elif rules["official_primary_source_can_stand_alone"] and official_primary:
                status = "single_authoritative"
                reasons.append("存在单一正式一级原始来源")
            elif len(origins) >= rules["min_independent_origins"]:
                status = "cross_verified"
                reasons.append(f"由 {len(origins)} 个独立信息源链支持")
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

            assessments.append(ClaimAssessment(
                claim_id=claim["id"], claim_text=claim["claim_text"], importance=claim["importance"],
                status=status, independent_origins=len(origins), supporting_sources=len(supporting),
                contradicting_sources=len(contradicting), reasons=reasons,
            ))

    blocking = set(rules["blocking_statuses"])
    blockers = [a.claim_id for a in assessments if a.importance == "critical" and a.status in blocking]
    return {
        "topic_id": topic_id,
        "draft_allowed": not blockers,
        "blocking_claims": blockers,
        "claims": [asdict(item) for item in assessments],
    }
