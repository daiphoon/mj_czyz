"""研究入口影子意见与回源推荐；不参与选择、排序或深研批准。"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

from .db import Database, now


CONTRACT = "candidate_shadow_v1"
STATUSES = ("eligible", "needs_evidence", "reject")
FIELDS = ("reason", "decisive_unknown", "verification_entry")
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": list(STATUSES)},
                   **{name: {"type": "string"} for name in FIELDS}},
    "required": ["status", *FIELDS],
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def attach_shadow(pool, data, material, attach):
    ranked = attach(pool, data)
    for event in ranked:
        raw = event.model_analysis.get("eligibility")
        valid = (isinstance(raw, dict) and raw.get("status") in STATUSES
                 and all(isinstance(raw.get(k), str) and raw[k].strip() for k in FIELDS))
        event.model_analysis["_eligibility"] = {
            **({k: raw[k] for k in ("status", *FIELDS)} if valid else {
                "status": "unavailable", "reason": "原调用未提供完整影子意见；不重试、不推定合格",
                "decisive_unknown": "未评估", "verification_entry": "未评估",
            }),
            "contract": CONTRACT, "mode": "shadow", "assessed_by": "screening_model",
            "assessed_at": material["frozen_at"], "material_sha256": material["prompt_sha256"],
        }
    return ranked


def candidate_hash(candidate):
    # 仅此摘要是候选材料版本；人工审查存于独立 task，不反向改变候选。
    return digest(asdict(candidate))


def current_candidate_review(db, run_id, candidate):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT result_json FROM tasks WHERE run_id=? AND kind='candidate_review' AND input_hash=? AND status='completed'",
            (run_id, digest([candidate.id, candidate_hash(candidate)])),
        ).fetchone()
    return json.loads(row[0]) if row else None


def register_candidate_review(settings, run_id, candidate_id, record_path):
    from .workflow import Workflow
    wf = Workflow(settings)
    candidate = next((c for c in wf.candidates(run_id) if c.id == candidate_id), None)
    if candidate is None:
        raise ValueError("候选不存在")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("candidate_sha256") != candidate_hash(candidate):
        raise ValueError("候选材料版本不匹配，须重新核对")
    if record.get("status") not in STATUSES:
        raise ValueError("无效研究入口状态")
    if record.get("recommendation") not in {"main", "backup", "lead_only", "stop"}:
        raise ValueError("无效人工推荐")
    if record["recommendation"] in {"main", "backup"} and record["status"] != "eligible":
        raise ValueError("主选/备选推荐须说明研究前提已成立")
    fields = ("reviewer", "facts", "current_mechanism", "historical_increment", "public_value",
              "authority_path", "decisive_unknown", "verification_entry", "investment_reason")
    if any(not isinstance(record.get(k), str) or not record[k].strip() for k in fields):
        raise ValueError("回源推荐依据不完整")
    reviewed_at = datetime.fromisoformat(record.get("reviewed_at", ""))
    if reviewed_at.tzinfo is None or reviewed_at > datetime.now(timezone.utc):
        raise ValueError("审查时间必须含时区且不在未来")
    refs = record.get("source_refs")
    if not isinstance(refs, list) or not refs or any(
        not isinstance(ref, dict) or any(not isinstance(ref.get(k), str) or not ref[k].strip()
                                       for k in ("url", "locator", "excerpt")) for ref in refs
    ):
        raise ValueError("须记录实际回源的URL、定位和必要摘录")
    key = digest([candidate_id, candidate_hash(candidate)])
    record.update(contract="candidate_review_v1", candidate_id=candidate_id, run_id=run_id,
                  note="记录内容审查者的推荐；程序仅检查字段和版本，不自动选题或核实事实")
    with wf.db.connect() as conn:
        conn.execute(
            """INSERT INTO tasks(id,run_id,kind,input_hash,status,result_json,updated_at)
               VALUES(?,?,'candidate_review',?,'completed',?,?)
               ON CONFLICT(run_id,kind,input_hash) DO UPDATE SET result_json=excluded.result_json,updated_at=excluded.updated_at""",
            (f"{run_id}:candidate_review:{key}", run_id, key, json.dumps(record, ensure_ascii=False), now()),
        )
    return record
