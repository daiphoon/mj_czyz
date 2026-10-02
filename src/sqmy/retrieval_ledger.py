"""搜索入口共用的调用账本；切换供应商不重置行为范围或未知消耗。"""
from dataclasses import asdict
import json
from .db import now
from .search_types import SearchError
from .tavily import atomic_json
from .transport_audit import TransportAudit


class RetrievalLedger:
    def __init__(self, settings, db, run_id, action):
        self.s, self.db, self.run_id, self.action = settings, db, run_id, action
        self.cfg = settings.raw.get("search", {})

    def previous(self, digest):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM retrieval_calls WHERE run_id=? AND action=? AND request_hash=? ORDER BY id DESC LIMIT 1", (self.run_id, self.action, digest)).fetchone()
        return dict(row) if row else None

    def reserve(self, provider, request, digest, *, cached=None, retry_count=0, fallback_from=None, endpoint="search"):
        if provider.paid and not self.cfg.get("allow_paid", False):
            raise SearchError("paid_mode_disabled", "configuration")
        cost = provider.cost_usd if provider.paid and cached is None else 0.0
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count, used = conn.execute("SELECT COUNT(*),COALESCE(SUM(cost_equivalent_usd),0) FROM retrieval_calls WHERE run_id=? AND action=?", (self.run_id, self.action)).fetchone()
            if count >= self.cfg.get("max_calls_per_action", 6):
                raise SearchError("action_request_limit", "budget")
            endpoint_count = conn.execute("SELECT COUNT(*) FROM retrieval_calls WHERE run_id=? AND action=? AND endpoint=?", (self.run_id, self.action, endpoint)).fetchone()[0]
            limit_key = "max_searches_per_action" if endpoint == "search" else "max_extracts_per_action"
            limit = min(self.cfg.get(limit_key, 4 if endpoint == "search" else 2), self.s.raw.get("tavily", {}).get(limit_key, 4 if endpoint == "search" else 2))
            if endpoint_count >= limit:
                raise SearchError("action_endpoint_limit", "budget")
            if used + cost > min(self.cfg.get("action_cost_limit_usd", 0.08), self.s.raw.get("tavily", {}).get("action_cost_limit_usd", 0.08)):
                raise SearchError("action_cost_limit", "budget")
            stamp = now()
            cursor = conn.execute("""INSERT INTO retrieval_calls(run_id,action,endpoint,request_hash,status,reserved_credits,
                accounted_credits,cost_equivalent_usd,accounting_method,result_json,created_at,updated_at,
                provider,auth_mode,intent,query_id,retry_count,fallback_from,cache_hit)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.run_id, self.action, endpoint, digest, "completed" if cached is not None else "running", 0, 0,
                 cost, "cache" if cached is not None else "free_keyless" if provider.auth_mode == "keyless" else "free_http" if not provider.paid else "reserved_estimate",
                 json.dumps(dict(cached, transport=TransportAudit(cache=True).snapshot()), ensure_ascii=False) if cached is not None else None, stamp, stamp,
                 provider.name, provider.auth_mode, request.intent.value, digest[:16], retry_count, fallback_from, int(cached is not None)))
            call_id = cursor.lastrowid
        self.export()
        return call_id

    def record_transport(self, call_id, transport):
        """派发前只更新本次预留，不改状态、不退款、不触及旧行。"""
        with self.db.connect() as conn:
            conn.execute("UPDATE retrieval_calls SET result_json=?,updated_at=? WHERE id=? AND status='running'",
                         (json.dumps({"results": [], "transport": transport}, ensure_ascii=False), now(), call_id))

    def finish(self, call_id, *, results=None, record=None, error=None, latency_ms=0, transport=None):
        value = {"results": [record] if record is not None else [asdict(r) for r in results] if results is not None else []}
        if transport is not None:
            value["transport"] = transport
        with self.db.connect() as conn:
            conn.execute("UPDATE retrieval_calls SET status=?,result_json=?,error_code=?,latency_ms=?,reported_credits=?,updated_at=? WHERE id=?",
                         ("failed" if error else "completed", json.dumps(value, ensure_ascii=False),
                          error.code if error else None, latency_ms, transport.get("reported_credits") if transport else None, now(), call_id))
        self.export()

    def export(self):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM retrieval_calls WHERE run_id=? ORDER BY id", (self.run_id,)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            result = json.loads(record.pop("result_json") or "{}")
            record["result_count"] = len(result.get("results", []))
            record["transport"] = result.get("transport")  # 无记录的旧行保持未知，不推断发送次数。
            records.append(record)
        atomic_json(self.s.root / "data/runs" / self.run_id / "retrieval_calls.json", records)
