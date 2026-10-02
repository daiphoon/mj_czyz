"""可选的有界搜索/提取通道：SQLite先占额，成功复用，失败不抹账。"""
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .db import now
from .materials import select_excerpt
from .snapshots import _public_http_url


class TavilyError(Exception):
    def __init__(self, code, status="failed"):
        self.code, self.status = code, status
        super().__init__(f"Tavily补充通道：{code}；已保存检查点，不自动换工具重试")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TavilyError("redirect_refused")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        temporary = stream.name
    os.replace(temporary, path)


def validate_config(cfg):
    integer_keys = ("max_searches_per_action", "max_results_per_query", "max_extracts_per_action",
                    "max_retries", "request_timeout_seconds", "max_response_bytes", "cache_ttl_hours",
                    "summary_chars", "excerpt_chars", "context_chars")
    for key in integer_keys:
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == "max_retries" else 1):
            raise ValueError(f"tavily.{key}必须为有效整数")
    for key in ("action_credit_limit", "action_cost_limit_usd", "credit_price_usd", "basic_search_credits",
                "advanced_search_credits", "basic_extract_credits_per_five", "advanced_extract_credits_per_five"):
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"tavily.{key}必须为有限正数")
    if cfg["max_results_per_query"] > 5 or cfg["request_timeout_seconds"] > 60 or cfg["summary_chars"] > 700:
        raise ValueError("Tavily单次结果/摘要/超时超过本项目边界")
    if any(cfg.get(key) not in {"basic", "advanced"} for key in ("search_depth", "extract_depth")):
        raise ValueError("Tavily深度只能使用已核验计价的basic或advanced")
    scenarios = cfg.get("scenario_queries", [])
    if not isinstance(scenarios, list) or any(not isinstance(s, dict) or not re.fullmatch(r"[a-z0-9_]+", str(s.get("id", "")))
            or not isinstance(s.get("query"), str) or not s["query"].strip() or len(s["query"]) > 500
            or not isinstance(s.get("signals"), list) or not s["signals"]
            or any(not isinstance(signal, str) or not signal for signal in s["signals"]) for s in scenarios):
        raise ValueError("Tavily场景须有唯一id、具体query和signals")
    if len({s["id"] for s in scenarios}) != len(scenarios):
        raise ValueError("Tavily场景id不得重复")
    roles = cfg.get('discovery_roles', [])
    if (not isinstance(roles, list) or any(role not in {'local', 'national', 'scene', 'exploration'} for role in roles)
            or len(roles) != len(set(roles)) or len(roles) > cfg['max_searches_per_action']
            or (roles and ({s.get('role') for s in scenarios} != set(roles)))):
        raise ValueError('发现查询角色须唯一、覆盖全部场景且不超过行为请求上限')


class TavilyClient:
    def __init__(self, settings, db, run_id, action):
        self.s, self.db, self.run_id, self.action = settings, db, run_id, action
        self.cfg = settings.raw.get("tavily", {})
        validate_config(self.cfg)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id) or not re.fullmatch(r"[A-Za-z0-9_:-]+", action):
            raise ValueError("无效的检索运行或行为标识")
        with db.connect() as conn:
            if not conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise ValueError("检索必须绑定已有运行")

    @property
    def available(self):
        return (self.cfg.get("enabled", False) and self.s.raw.get("search", {}).get("allow_paid", False)
                and bool(os.environ.get("TAVILY_API_KEY")))

    def _text(self, value, limit):
        text = str(value or "")
        key = os.environ.get("TAVILY_API_KEY")
        if key:
            text = text.replace(key, "[REDACTED]")
        text = re.sub(r"tvly-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
        return text[:limit]

    def search(self, query, *, purpose="discovery", start_date=None, end_date=None, retry=False):
        if purpose not in {"discovery", "policy"} or not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("搜索须有500字以内的具体问题和明确用途")
        if self._text(query, 500) != query:
            raise ValueError("检索词不得含凭证")
        payload = {"query": query, "topic": "news" if purpose == "discovery" else "general",
                   "search_depth": self.cfg["search_depth"], "max_results": self.cfg["max_results_per_query"],
                   "include_answer": False, "include_raw_content": False, "include_images": False,
                   "auto_parameters": False, "include_usage": True}
        if purpose == "discovery":
            for field, value in (("start_date", start_date), ("end_date", end_date)):
                if not value:
                    raise ValueError("发现搜索必须固定开始和结束日期")
                datetime.strptime(value, "%Y-%m-%d")
                payload[field] = value
            if start_date > end_date:
                raise ValueError("开始日期不得晚于结束日期")
        # 政策检索不沿用新闻窗口；返回结果仍须检查有效期和适用范围。
        reserve = self.cfg[f"{self.cfg['search_depth']}_search_credits"]
        return self._invoke("search", payload, reserve, retry=retry)

    def extract(self, url, terms, *, reason, retry=False):
        _public_http_url(url)
        if not isinstance(terms, list) or not 1 <= len(terms) <= 8 or any(not isinstance(t, str) or not t.strip() or len(t) > 100 for t in terms):
            raise ValueError("定向提取需要1—8个具体目标词")
        if reason not in {"http_failed", "http_incomplete"}:
            raise ValueError("先尝试普通HTTP，只对失败或不完整的核心公开页面补提取")
        if any(self._text(value, len(value)) != value for value in [url, *terms]):
            raise ValueError("提取输入不得含凭证")
        payload = {"urls": [url], "extract_depth": self.cfg["extract_depth"], "format": "text",
                   "timeout": self.cfg["request_timeout_seconds"], "include_usage": True}
        # 不让供应商只回相关片段后丢失条件；单页有界响应在内存按目标+条件摘录，全文不落盘。
        reserve = self.cfg[f"{self.cfg['extract_depth']}_extract_credits_per_five"]
        return self._invoke("extract", payload, reserve, terms=terms, retry=retry)

    def _post(self, endpoint, payload):
        if not self.s.raw.get("search", {}).get("allow_paid", False):
            raise TavilyError("paid_mode_disabled", "needs_review")
        request = Request("https://api.tavily.com/" + endpoint, json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"], "Content-Type": "application/json"})
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.cfg["request_timeout_seconds"]) as response:
                raw = response.read(self.cfg["max_response_bytes"] + 1)
        except HTTPError as exc:
            # 不记录响应原文/请求headers，防止服务端错误回显凭证。
            status = "paused_quota" if exc.code in {429, 432, 433} else "needs_review" if exc.code == 401 else "failed"
            raise TavilyError(f"http_{exc.code}", status) from None
        if len(raw) > self.cfg["max_response_bytes"]:
            raise TavilyError("response_too_large")
        return json.loads(raw)

    def _normalize(self, endpoint, data, payload, terms):
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise TavilyError("invalid_response")
        results = []
        for row in data["results"][:self.cfg["max_results_per_query"] if endpoint == "search" else 1]:
            if not isinstance(row, dict):
                raise TavilyError("invalid_result")
            url = self._text(row.get("url"), 2048)
            try:
                _public_http_url(url)
            except ValueError:
                continue
            item = {"url": url}
            if endpoint == "search":
                item.update(title=self._text(row.get("title"), 350), summary=self._text(row.get("content"), self.cfg["summary_chars"]),
                            published_at=self._text(row.get("published_date"), 120), updated_at=self._text(row.get("updated_date"), 120))
                if not item["title"]:
                    continue
            else:
                if url.rstrip("/") != payload["urls"][0].rstrip("/"):
                    continue
                text = self._text(row.get("raw_content"), self.cfg["max_response_bytes"])
                if not text.strip():
                    continue
                item.update(select_excerpt(text, terms, self.cfg))
            results.append(item)
        return {"results": results, "request_id": self._text(data.get("request_id"), 100),
                "needs_review": True, "failed_count": len(payload.get("urls", [])) - len(results) if endpoint == "extract" else 0}

    def _invoke(self, endpoint, payload, reserve, *, terms=None, retry=False):
        identity = {"version": 1, "endpoint": endpoint, "payload": payload, "terms": terms,
                    "summary_chars": self.cfg["summary_chars"], "excerpt_chars": self.cfg["excerpt_chars"], "context_chars": self.cfg["context_chars"]}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        directory = self.s.root / "data/runs" / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / (".retrieval-" + self.action.replace(":", "-") + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TavilyError("action_busy") from None
            return self._locked_call(endpoint, payload, reserve, digest, terms, retry)

    def _locked_call(self, endpoint, payload, reserve, digest, terms, retry):
        stamp = now()
        # 已取得行为锁，遗留running请求不再有活动进程；用量仍未知，不释放占额。
        with self.db.connect() as conn:
            conn.execute("UPDATE retrieval_calls SET status='needs_review',error_code='interrupted_unknown',accounting_method='conservative_estimate',updated_at=? WHERE run_id=? AND action=? AND request_hash=? AND status='running'",
                         (stamp, self.run_id, self.action, digest))
        self.export_audit()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM retrieval_calls WHERE run_id=? AND action=? AND request_hash=? ORDER BY id DESC",
                               (self.run_id, self.action, digest)).fetchall()
            if old and old[0]["status"] == "completed":
                return dict(json.loads(old[0]["result_json"]), call_id=old[0]["id"], reused=True)
            if old and (not retry or len(old) > self.cfg["max_retries"]):
                raise TavilyError(old[0]["error_code"] or "interrupted_unknown", "needs_review")
            if not self.available:
                raise TavilyError("disabled_or_key_missing", "needs_review")
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=self.cfg["cache_ttl_hours"])).isoformat()
            cached = conn.execute("SELECT r.* FROM retrieval_calls r JOIN run_context rc ON rc.run_id=r.run_id WHERE r.request_hash=? AND r.status='completed' AND r.cached_from IS NULL AND r.updated_at>=? AND rc.mode IN ('live','diagnostic') ORDER BY r.id DESC LIMIT 1",
                                  (digest, cutoff)).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM retrieval_calls WHERE run_id=? AND action=? AND endpoint=?", (self.run_id, self.action, endpoint)).fetchone()[0]
            if count >= self.cfg["max_searches_per_action" if endpoint == "search" else "max_extracts_per_action"]:
                raise TavilyError("action_request_limit", "paused_budget")
            used, cost = conn.execute("SELECT COALESCE(SUM(accounted_credits),0),COALESCE(SUM(cost_equivalent_usd),0) FROM retrieval_calls WHERE run_id=? AND action=?",
                                     (self.run_id, self.action)).fetchone()
            reserved = 0.0 if cached else reserve
            price = self.cfg["credit_price_usd"]
            if used + reserved > self.cfg["action_credit_limit"] or cost + reserved * price > self.cfg["action_cost_limit_usd"]:
                raise TavilyError("action_credit_or_cost_limit", "paused_budget")
            cursor = conn.execute("""INSERT INTO retrieval_calls(run_id,action,endpoint,request_hash,status,reserved_credits,
                                  accounted_credits,cost_equivalent_usd,accounting_method,result_json,cached_from,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (self.run_id, self.action, endpoint, digest, "completed" if cached else "running", reserved,
                                   reserved, reserved * price, "cache" if cached else "reserved_estimate", cached["result_json"] if cached else None,
                                   cached["id"] if cached else None, stamp, stamp))
            call_id = cursor.lastrowid
        self.export_audit()
        if cached:
            return dict(json.loads(cached["result_json"]), call_id=call_id, reused=True)
        data, result, failure = None, None, None
        try:
            data = self._post(endpoint, payload)
            result = self._normalize(endpoint, data, payload, terms)
            if endpoint == "extract" and not result["results"]:
                raise TavilyError("extract_unavailable")
        except (Exception, KeyboardInterrupt) as exc:
            failure = exc
        usage = data.get("usage", {}).get("credits") if isinstance(data, dict) and isinstance(data.get("usage", {}), dict) else None
        known = isinstance(usage, (int, float)) and not isinstance(usage, bool) and math.isfinite(usage) and usage >= 0
        accounted = float(usage) if known else reserve
        method = "provider_reported" if known else "conservative_estimate"
        if known and endpoint == "extract" and result:
            # Extract可在凑满5个成功页面前返回0；保留按成功页分摊的费用，不能当免费。
            accrued = reserve * len(result["results"]) / 5
            if accrued > accounted:
                accounted, method = accrued, "reported_plus_fractional_accrual"
        code = getattr(failure, "code", type(failure).__name__) if failure else None
        status = getattr(failure, "status", "failed") if failure else "completed"
        if result is not None and (used + accounted > self.cfg["action_credit_limit"] or cost + accounted * price > self.cfg["action_cost_limit_usd"]):
            result["budget_overrun"] = {"reserved_credits": reserve, "accounted_credits": accounted,
                "reason": "实际记账超过调用前预留并超出本行为限制；保存结果，后续请求须先调整预算"}
        with self.db.connect() as conn:
            conn.execute("UPDATE retrieval_calls SET status=?,reported_credits=?,accounted_credits=?,cost_equivalent_usd=?,accounting_method=?,result_json=?,error_code=?,updated_at=? WHERE id=?",
                         (status, usage if known else None, accounted, accounted * price, method,
                          json.dumps(result, ensure_ascii=False) if result else None, code, now(), call_id))
        self.export_audit()
        if failure:
            if isinstance(failure, KeyboardInterrupt):
                raise failure
            raise TavilyError(code, status) from None
        return dict(result, call_id=call_id, reused=False)

    def export_audit(self):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM retrieval_calls WHERE run_id=? ORDER BY id", (self.run_id,)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            result = json.loads(record.pop("result_json") or "{}")
            record["result_count"] = len(result.get("results", []))
            records.append(record)
        atomic_json(self.s.root / "data/runs" / self.run_id / "retrieval_calls.json", records)


def retrieval_usage(db, run_id):
    with db.connect() as conn:
        rows = conn.execute("""SELECT action,COUNT(*) AS requests,
            SUM(CASE WHEN cached_from IS NOT NULL THEN 1 ELSE 0 END) AS cache_hits,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(reported_credits) AS reported_credits,SUM(accounted_credits) AS accounted_credits,
            SUM(cost_equivalent_usd) AS cost_equivalent_usd
            FROM retrieval_calls WHERE run_id=? GROUP BY action""", (run_id,)).fetchall()
    return {"run_id": run_id, "actions": [dict(row) for row in rows], "note": "积分及等价费用与模型Token分列；非账户实际账单，未知用量保守占额"}
