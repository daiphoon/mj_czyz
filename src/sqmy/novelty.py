from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import tomllib
from typing import Any

from .collector import SourceCollector
from .config import Settings
from .db import Database, now
from .models import EventItem
from .screener import normalize_title


ABSENCE_SIGNALS = ("尚未", "未建立", "没有", "缺少", "缺乏", "空白")
COVERAGE_SIGNALS = ("建立", "完善", "出台", "印发", "实施", "试点", "指引", "司法建议", "双向反馈", "协同机制")
GAP_TYPES = {"policy_absence", "implementation_gap", "coordination_gap", "effectiveness_gap", "accountability_gap", "unclear"}
REVIEW_OUTCOMES = {"confirmed", "reversed", "reframed", "not_reviewed"}


@dataclass
class NoveltyAudit:
    event_id: str
    title: str
    gap_hypothesis: str
    gap_type: str
    coverage_status: str
    decision: str
    queries: list[str]
    counterevidence: list[dict[str, Any]]
    policy_matches: list[dict[str, Any]]
    audit_tokens: int = 0
    potential_waste_tokens: int = 0


class NoveltyAuditor:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cfg = settings.section("novelty")
        self.db = Database(settings.database_path)
        self.db.initialize()
        self.collector = SourceCollector(settings.root, settings.raw)
        self._sync_policy_mechanisms()

    def _sync_policy_mechanisms(self) -> None:
        path = self.s.root / "config/policy_mechanisms.toml"
        if not path.exists():
            return
        with path.open("rb") as fh:
            mechanisms = tomllib.load(fh).get("mechanisms", [])
        with self.db.connect() as conn:
            for item in mechanisms:
                conn.execute(
                    """INSERT INTO policy_mechanisms(
                         id,name,problem_type,jurisdiction,actor,summary,keywords_json,
                         source_url,valid_from,last_verified_at,active
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(id) DO UPDATE SET
                         name=excluded.name,problem_type=excluded.problem_type,
                         jurisdiction=excluded.jurisdiction,actor=excluded.actor,
                         summary=excluded.summary,keywords_json=excluded.keywords_json,
                         source_url=excluded.source_url,valid_from=excluded.valid_from,
                         last_verified_at=excluded.last_verified_at,active=1""",
                    (
                        item["id"], item["name"], item["problem_type"], item["jurisdiction"],
                        item["actor"], item["summary"], json.dumps(item["keywords"], ensure_ascii=False),
                        item["source_url"], item.get("valid_from"), item["last_verified_at"],
                    ),
                )

    def audit(self, run_id: str, events: list[EventItem], *, live_search: bool) -> list[NoveltyAudit]:
        audits = [self._audit_one(event, live_search=live_search) for event in events[: self.cfg["audit_pool_size"]]]
        with self.db.connect() as conn:
            for audit in audits:
                conn.execute(
                    """INSERT INTO novelty_audits(
                         id,run_id,event_id,title,gap_hypothesis,gap_type,coverage_status,
                         decision,queries_json,counterevidence_json,policy_matches_json,
                         audit_tokens,potential_waste_tokens,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,event_id) DO UPDATE SET
                         title=excluded.title,gap_hypothesis=excluded.gap_hypothesis,
                         gap_type=excluded.gap_type,coverage_status=excluded.coverage_status,
                         decision=excluded.decision,queries_json=excluded.queries_json,
                         counterevidence_json=excluded.counterevidence_json,
                         policy_matches_json=excluded.policy_matches_json,
                         audit_tokens=excluded.audit_tokens,
                         potential_waste_tokens=excluded.potential_waste_tokens""",
                    (
                        f"{run_id}:{audit.event_id}", run_id, audit.event_id, audit.title,
                        audit.gap_hypothesis, audit.gap_type, audit.coverage_status, audit.decision,
                        json.dumps(audit.queries, ensure_ascii=False),
                        json.dumps(audit.counterevidence, ensure_ascii=False),
                        json.dumps(audit.policy_matches, ensure_ascii=False), audit.audit_tokens,
                        audit.potential_waste_tokens, now(),
                    ),
                )
        self._write_run_audit(run_id, audits)
        return audits

    def _audit_one(self, event: EventItem, *, live_search: bool) -> NoveltyAudit:
        analysis = getattr(event, "model_analysis", {})
        gap = analysis.get("gap_hypothesis") or analysis.get("policy_gap") or "需核对现行机制是否已覆盖"
        gap_type = analysis.get("gap_type", "unclear")
        if gap_type not in GAP_TYPES:
            gap_type = "unclear"
        queries = [self._official_query(str(q).strip()) for q in analysis.get("counter_queries", []) if str(q).strip()]
        queries = queries[: self.cfg["max_queries_per_candidate"]]
        text = " ".join((
            event.title, event.summary, gap, analysis.get("policy_gap", ""),
            analysis.get("policy_entry", ""), analysis.get("institutional_issue", ""),
        ))
        policy_matches = self._policy_matches(text)
        counterevidence: list[dict[str, Any]] = []
        seen_hits: set[str] = set()

        if live_search:
            for query in queries:
                try:
                    hits = self.collector.search_query(query, limit=self.cfg["max_hits_per_query"])
                except Exception as exc:
                    counterevidence.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                for hit in hits:
                    normalized_hit = normalize_title(hit.title)
                    if hit.url == event.url or SequenceMatcher(None, normalized_hit, normalize_title(event.title)).ratio() >= 0.92:
                        continue
                    origin_group = normalized_hit
                    if origin_group in seen_hits:
                        continue
                    seen_hits.add(origin_group)
                    hit_text = hit.title + " " + hit.summary
                    if hit.source_level == 1 and any(signal in hit_text for signal in COVERAGE_SIGNALS):
                        counterevidence.append({
                            "query": query, "title": hit.title, "url": hit.url,
                            "source_level": hit.source_level, "summary": hit.summary[:400],
                            "origin_group": origin_group,
                        })

        absence_claim = gap_type == "policy_absence" or (
            gap_type == "unclear" and any(signal in gap for signal in ABSENCE_SIGNALS)
        )
        fresh_policy_matches = [item for item in policy_matches if item["fresh"]]
        if fresh_policy_matches and absence_claim:
            status, decision = "covered", "block_original_gap"
        elif policy_matches or counterevidence:
            status, decision = "likely_covered", "keep_with_novelty_warning"
        else:
            status, decision = "unclear", "proceed_limited_research"
        potential = self.cfg["potential_waste_tokens_per_block"] if decision == "block_original_gap" else 0
        return NoveltyAudit(
            event_id=event.id, title=event.title, gap_hypothesis=gap, gap_type=gap_type,
            coverage_status=status, decision=decision, queries=queries,
            counterevidence=counterevidence, policy_matches=policy_matches,
            potential_waste_tokens=potential,
        )

    def _official_query(self, query: str) -> str:
        if "site:" in query:
            return query
        domains = self.cfg["official_counterevidence_domains"]
        scope = " OR ".join(f"site:{domain}" for domain in domains)
        return f"{query} ({scope})"

    def _policy_matches(self, text: str) -> list[dict[str, Any]]:
        matches = []
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM policy_mechanisms WHERE active=1").fetchall()
        for row in rows:
            keywords = json.loads(row["keywords_json"])
            matched = [word for word in keywords if word in text]
            if len(matched) >= self.cfg["local_mechanism_min_keyword_matches"]:
                verified = datetime.fromisoformat(row["last_verified_at"]).replace(tzinfo=timezone.utc)
                fresh = datetime.now(timezone.utc) - verified <= timedelta(days=self.cfg["mechanism_max_age_days"])
                matches.append({
                    "id": row["id"], "name": row["name"], "matched_keywords": matched,
                    "summary": row["summary"], "source_url": row["source_url"],
                    "last_verified_at": row["last_verified_at"],
                    "fresh": fresh,
                })
        return matches

    def _write_run_audit(self, run_id: str, audits: list[NoveltyAudit]) -> None:
        path = self.s.root / "data/runs" / run_id / "novelty_audit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps([asdict(item) for item in audits], ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)


def record_review(settings: Settings, audit_id: str, outcome: str, reason: str) -> None:
    if outcome not in REVIEW_OUTCOMES - {"not_reviewed"}:
        raise ValueError(f"不支持的复核结果：{outcome}")
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE novelty_audits SET review_outcome=?,review_reason=?,reviewed_at=? WHERE id=?",
            (outcome, reason, now(), audit_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"未找到审计记录：{audit_id}")


def rolling_evaluation(settings: Settings, days: int | None = None) -> dict[str, Any]:
    cfg = settings.section("novelty")
    window_days = days or cfg["rolling_evaluation_days"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT n.* FROM novelty_audits n
               JOIN run_context rc ON rc.run_id=n.run_id
               WHERE n.created_at>=? AND rc.mode='live' ORDER BY n.created_at""",
            (cutoff,),
        ).fetchall()
        screening_tokens = conn.execute(
            """SELECT COALESCE(SUM(m.input_tokens+m.output_tokens),0)
               FROM model_calls m JOIN run_context rc ON rc.run_id=m.run_id
               WHERE m.created_at>=? AND m.task_id='screening' AND rc.mode='live'""",
            (cutoff,),
        ).fetchone()[0]
        efficiency = conn.execute(
            """SELECT COALESCE(SUM(e.premodel_count),0),
                      COALESCE(SUM(e.repeated_excluded),0),
                      COALESCE(SUM(e.model_input_count),0),
                      COALESCE(SUM(e.screening_cache_hit),0),
                      COALESCE(SUM(e.screening_tokens_saved),0),
                      COALESCE(SUM(e.deferred_count),0)
               FROM run_efficiency e
               JOIN runs r ON r.id=e.run_id JOIN run_context rc ON rc.run_id=e.run_id
               WHERE r.created_at>=? AND rc.mode='live'""",
            (cutoff,),
        ).fetchone()
    total = len(rows)
    counts = {status: sum(row["coverage_status"] == status for row in rows) for status in ("covered", "likely_covered", "unclear")}
    blocked = [row for row in rows if row["decision"] == "block_original_gap"]
    reviewed = [row for row in blocked if row["review_outcome"]]
    confirmed = sum(row["review_outcome"] == "confirmed" for row in reviewed)
    reversed_count = sum(row["review_outcome"] == "reversed" for row in reviewed)
    observed_rate = len(reviewed) / len(blocked) if blocked else None
    precision = confirmed / len(reviewed) if reviewed else None
    enough_volume = total >= cfg["minimum_audits_for_decision"]
    enough_reviews = bool(blocked) and len(reviewed) >= cfg["minimum_reviewed_blocks_for_precision"] and observed_rate is not None and observed_rate >= cfg["minimum_observed_outcome_rate"]
    if not enough_volume:
        conclusion = "insufficient_sample"
    elif not blocked:
        conclusion = "no_block_cases_yet"
    elif blocked and not enough_reviews:
        conclusion = "needs_more_review_labels"
    elif precision is not None and reversed_count:
        conclusion = "review_false_blocks_before_tightening"
    else:
        conclusion = "continue_current_gate"
    return {
        "window_days": window_days, "audit_count": total, "status_counts": counts,
        "blocked_count": len(blocked), "reviewed_block_count": len(reviewed),
        "confirmed_block_count": confirmed, "reversed_block_count": reversed_count,
        "observed_outcome_rate": round(observed_rate, 4) if observed_rate is not None else None, "observed_block_precision": precision,
        "audit_tokens": sum(row["audit_tokens"] for row in rows),
        "screening_tokens": int(screening_tokens),
        "premodel_count": int(efficiency[0]),
        "repeated_events_excluded_before_model": int(efficiency[1]),
        "model_input_count": int(efficiency[2]),
        "screening_cache_hits": int(efficiency[3]),
        "screening_tokens_saved_by_cache": int(efficiency[4]),
        "events_deferred_for_batch": int(efficiency[5]),
        "potential_waste_tokens_prevented": sum(row["potential_waste_tokens"] for row in blocked),
        "measurement_note": "potential_waste_tokens_prevented是代理指标，不等于实际账单节省；误杀率需要人工或后续研究结果标签。",
        "automatic_conclusion": conclusion,
    }


def write_rolling_evaluation(settings: Settings, days: int | None = None) -> Path:
    result = rolling_evaluation(settings, days)
    path = settings.root / "outputs/review/metrics" / f"novelty_rolling_{result['window_days']}d.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path
