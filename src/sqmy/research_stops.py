"""发现前读取研究停止记录；证据质量仍由有版本的人工预研复核裁决。"""
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import re

from .collector import canonical_url
from .materials import split_discovery_summary
from .source_trace import read_issue


def event_signature(event):
    content = [canonical_url(event.url), event.title.strip(),
               split_discovery_summary(event.summary)["reported_excerpt"].strip()]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()


def _text(value):
    return re.sub(r"[\W_]+", "", value.lower())


def _source_signature(source):
    return (canonical_url(source['url']) if source.get('url') else '', source.get("excerpt", "").strip())


def reopen_errors(payload):
    assessment = payload.get("reopen_assessment")
    if assessment is None:
        return []
    if not isinstance(assessment, dict):
        return ["reopen_assessment 必须为对象"]
    errors = []
    for key in ("stop_ids", "new_evidence_claim_ids"):
        values = assessment.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            errors.append(f"重开须填写非空 {key}")
    if not re.fullmatch(r"[a-f0-9]{64}", str(assessment.get("event_content_sha256", ""))):
        errors.append("重开须绑定发现材料 event_content_sha256")
    if not isinstance(assessment.get("condition_met"), str) or not assessment["condition_met"].strip():
        errors.append("重开须说明新证据怎样满足原停止条件")
    if assessment.get("basis") not in {"new_evidence", "correction"}:
        errors.append("重开依据只能为 new_evidence/correction")
    if assessment.get("basis") == "correction" and not str(assessment.get("correction_reason", "")).strip():
        errors.append("纠正原判断须说明 correction_reason")
    claims = {c.get("id"): c for c in payload.get("verified_facts", []) if isinstance(c, dict)}
    sources = {s.get("key"): s for s in payload.get("sources", []) if isinstance(s, dict)}
    for claim_id in assessment.get("new_evidence_claim_ids", []) if isinstance(assessment.get("new_evidence_claim_ids"), list) else []:
        claim = claims.get(claim_id)
        if not claim or not claim.get("source_keys"):
            errors.append("重开证据须引用 verified_facts 的主张和来源")
            continue
        for key in claim["source_keys"]:
            source = sources.get(key)
            if (not source or source.get("source_level") not in {1, 2}
                    or source.get("fetch_status") not in {"excerpt_verified", "fulltext_ok"}
                    or read_issue(source)):
                errors.append("重开来源须已读必要正文、记录定位且为一级或二级")
    return errors


@dataclass
class Stop:
    id: str
    topic_id: str
    occurred_at: str
    summary: str
    condition: str
    title: str
    synopsis: str
    urls: set
    sources: set


class ResearchStopGate:
    def __init__(self, db):
        with db.connect() as conn:
            candidates = conn.execute("SELECT run_id,id,title,data_json FROM candidates").fetchall()
            self.reviews = [dict(r) for r in conn.execute("""SELECT rr.* FROM research_reviews rr
                JOIN run_context rc ON rc.run_id=rr.run_id WHERE rc.mode='live'
                ORDER BY rr.created_at,rr.rowid""")]
            tasks = [dict(r) for r in conn.execute("""SELECT t.* FROM tasks t
                JOIN run_context rc ON rc.run_id=t.run_id WHERE rc.mode='live'
                AND t.status='completed' AND t.kind IN ('research_brief','research_stop')
                ORDER BY t.rowid""")]
        by_candidate = {(r["run_id"], r["id"].removeprefix(r["run_id"] + ":")): json.loads(r["data_json"]) for r in candidates}
        by_review = {r["id"]: r for r in self.reviews}
        self.latest = {(r["run_id"], r["candidate_id"]): r["id"] for r in self.reviews}
        self.stops = {}

        def add(stop_id, row, payload, stamp, summary, condition):
            candidate = by_candidate.get((row["run_id"], payload.get("candidate_id", row.get("candidate_id"))), {})
            sources = payload.get("sources", [])
            candidate_url = candidate.get("score_reasons", {}).get("来源URL", "")
            urls = {canonical_url(candidate_url)} if candidate_url else set()
            urls.update(canonical_url(s.get("url", "")) for s in sources
                        if s.get("source_role") in {"media_investigation", "court_case", "official_case", "media_report"})
            self.stops[stop_id] = Stop(stop_id, payload.get("topic_id", ""), stamp, summary, condition,
                candidate.get("title", payload.get("working_title", "")), candidate.get("summary", ""),
                urls - {""}, {_source_signature(s) for s in sources})

        for row in self.reviews:
            payload = json.loads(row["data_json"])
            if row["decision"] == "stop":
                add("review:" + row["id"], row, payload, row["created_at"],
                    payload.get("stop_summary", payload.get("blocking_assessment", row["decision"])),
                    payload.get("reopen_condition", "须取得决定性新证据或原判断纠错依据并重新人工复核"))
            if row["human_decision"] == "stop":
                stamp = row['reviewed_at'] or row['created_at']
                add("human:" + row["id"] + ":" + stamp, row, payload, stamp,
                    row["human_note"] or '人工停止（旧记录缺少理由）', payload.get("reopen_condition", "须取得决定性新证据或原判断纠错依据并重新人工复核"))
        for row in tasks:
            payload = json.loads(row["result_json"])
            if row["kind"] == "research_stop":
                add(payload["stop_id"], row, payload["review_payload"], row["updated_at"],
                    payload["summary"], payload["condition"])
            elif payload.get("outcome") == "no_draft":
                pre = by_review.get(payload.get("pre_research_review_id"), {})
                context = json.loads(pre.get("data_json", "{}")) | payload
                add("brief:" + row["id"], row, context, row["updated_at"],
                    payload.get("conclusion", "深研不成稿"), payload.get("reopen_condition", "须重新核验关键证据"))

    def _match(self, event, stop):
        hints = {canonical_url(event.url)}
        original = event.material.get("original_source_hint", "")
        if isinstance(original, str) and original.startswith(("http://", "https://")):
            hints.add(canonical_url(original))
        if hints & stop.urls:
            return "origin_url"
        title_ratio = SequenceMatcher(None, _text(event.title), _text(stop.title)).ratio()
        if stop.title and title_ratio >= 0.82:
            return "same_problem_title"
        # 转载换标题只在标题和具体材料陈述同时高度重合时匹配；不按主题词永久封禁。
        a = _text(split_discovery_summary(event.summary)["reported_excerpt"])
        b = _text(split_discovery_summary(stop.synopsis)["reported_excerpt"])
        if len(a) >= 40 and len(b) >= 40 and title_ratio >= 0.45:
            grams_a = {a[i:i+4] for i in range(len(a)-3)}
            grams_b = {b[i:i+4] for i in range(len(b)-3)}
            shared = grams_a & grams_b
            if len(shared) >= 12 and len(shared) / min(len(grams_a), len(grams_b)) >= 0.60:
                return "same_problem_excerpt"
        return None

    def _reopen(self, event, stop):
        for row in reversed(self.reviews):
            if (self.latest[(row["run_id"], row["candidate_id"])] != row["id"]
                    or not row["research_allowed"] or row["human_decision"] != "proceed"
                    or row["created_at"] <= stop.occurred_at or not row["reviewed_at"]):
                continue
            payload = json.loads(row["data_json"])
            assessment = payload.get("reopen_assessment") or {}
            if (reopen_errors(payload) or stop.id not in assessment.get("stop_ids", [])
                    or assessment.get("event_content_sha256") != event_signature(event)):
                continue
            if assessment.get("basis") == "new_evidence":
                claims = [c for c in payload.get("verified_facts", []) if c.get("id") in assessment["new_evidence_claim_ids"]]
                keys = {k for c in claims for k in c.get("source_keys", [])}
                fresh = [s for s in payload.get("sources", []) if s.get("key") in keys and _source_signature(s) not in stop.sources]
                if not fresh:
                    continue
            return row["id"]
        return None

    def partition(self, events):
        allowed, excluded, decisions = [], [], []
        for event in events:
            event.material.pop('research_history', None)
            matches = [(stop, self._match(event, stop)) for stop in self.stops.values()]
            matches = [(s, why) for s, why in matches if why]
            if not matches:
                allowed.append(event)
                continue
            reopen = {s.id: self._reopen(event, s) for s, _ in matches}
            permitted = all(reopen.values())
            decision = {"event_id": event.id, "event_content_sha256": event_signature(event),
                "title": event.title, "url": event.url, "published_at": event.published_at,
                "reported_excerpt": split_discovery_summary(event.summary)['reported_excerpt'][:350],
                "status": "reopened" if permitted else "research_stopped",
                "stop_ids": list(reopen), "matched_by": [why for _, why in matches],
                "stop_summary": [s.summary for s, _ in matches],
                "reopen_condition": [s.condition for s, _ in matches],
                "reopen_review_ids": [r for r in reopen.values() if r]}
            decisions.append(decision)
            if permitted:
                event.material["research_history"] = decision
                allowed.append(event)
            else:
                excluded.append({"event": event, "reason_code": "research_stop", "research_history": decision})
        return allowed, excluded, decisions


def preserve_manual_stop(db, review, reviewed_at, note):
    """人工 stop 的不可变快照，后来对同一预研行改决定也不会抹去停止历史。"""
    payload = json.loads(review["data_json"])
    stop_id = "human:" + review["id"] + ":" + reviewed_at
    record = {"stop_id": stop_id, "review_payload": payload, "summary": note,
              "condition": payload.get("reopen_condition", "须取得决定性新证据或原判断纠错依据并重新人工复核")}
    key = hashlib.sha256(stop_id.encode()).hexdigest()
    with db.connect() as conn:
        conn.execute("""INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at)
            VALUES(?,?,'research_stop',?,'completed',?,?) ON CONFLICT(run_id,kind,input_hash) DO NOTHING""",
            (review["run_id"] + ":research_stop:" + key, review["run_id"], key,
             json.dumps(record, ensure_ascii=False), reviewed_at))
