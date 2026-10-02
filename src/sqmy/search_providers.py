"""固定端点适配；keyless请求不读取或附带账户认证，也不升级为收费模式。"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .search_types import RetrievalIntent, SearchError, SearchRequest, SearchResult
from .snapshots import _public_http_url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SearchError("redirect_refused", "invalid_response")


def _delay(headers):
    value = headers.get("Retry-After") or headers.get("X-RateLimit-Reset")
    try:
        return max(0.0, min(float(value), 86400.0))  # Brave Reset是相对秒。
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0, min((stamp - datetime.now(timezone.utc)).total_seconds(), 86400))
        except (ValueError, TypeError):
            return None


def send_json(request, *, timeout, max_bytes):
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except HTTPError as exc:
        category = ("authentication" if exc.code in {401, 403} else "rate_limit" if exc.code == 429
                    else "quota" if exc.code in {432, 433} else "endpoint_unavailable" if exc.code in {404, 410}
                    else "transient" if exc.code >= 500 else "invalid_query")
        raise SearchError(f"http_{exc.code}", category, http_status=exc.code, retry_after=_delay(exc.headers)) from None
    except (TimeoutError, URLError, OSError):
        raise SearchError("transport_failed", "transient") from None
    if len(raw) > max_bytes:
        raise SearchError("response_too_large", "invalid_response")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SearchError("invalid_response_or_keyless_limit", "invalid_response") from None
    if not isinstance(data, dict):
        raise SearchError("invalid_response_or_keyless_limit", "invalid_response")
    return data


def _url(value):
    if not isinstance(value, str):
        return None
    try:
        return _public_http_url(value)
    except ValueError:
        return None


class BraveSearchProvider:
    name, auth_mode, paid = "brave", "keyed", True
    search_types = frozenset({"web"})

    def __init__(self, cfg, *, allow_paid=False):
        self.cfg, self.allow_paid = cfg, allow_paid
        self.parameters = {"language": "zh-hans", "max_results": cfg.get("max_results", 5)}
        self.cost_usd = float(cfg.get("cost_usd_per_search", 0.005))

    @property
    def available(self):
        return bool(self.cfg.get("enabled", False) and self.allow_paid and os.environ.get("BRAVE_SEARCH_API_KEY"))

    def search(self, request):
        if not self.available:
            raise SearchError("paid_mode_disabled_or_key_missing", "configuration")
        key = os.environ["BRAVE_SEARCH_API_KEY"]
        if key in request.query:
            raise SearchError("credential_in_query", "invalid_query")
        query = request.query
        if request.domains:
            query += " (" + " OR ".join("site:" + d for d in request.domains) + ")"
        query += "".join(" -site:" + d for d in request.exclude_domains)
        params = {"q": query, "count": min(request.max_results, self.cfg.get("max_results", 5)), "search_lang": "zh-hans"}
        if request.start_date or request.recency_days:
            end = request.end_date or datetime.now(timezone.utc).date().isoformat()
            start = request.start_date or (datetime.fromisoformat(end) - timedelta(days=request.recency_days)).date().isoformat()
            params["freshness"] = start + "to" + end
        req = Request("https://api.search.brave.com/res/v1/web/search?" + urlencode(params), headers={"X-Subscription-Token": key, "Accept": "application/json"})
        data = send_json(req, timeout=self.cfg.get("request_timeout_seconds", 20), max_bytes=self.cfg.get("max_response_bytes", 2_000_000))
        rows = data.get("web", {}).get("results") if isinstance(data.get("web"), dict) else None
        # 缺web字段不是可靠的空搜索结果。
        if not isinstance(rows, list):
            raise SearchError("missing_web_results", "invalid_response")
        return [SearchResult(str(r.get("title", ""))[:350].replace(key, "[REDACTED]"), _url(r.get("url")),
                             str(r.get("description", ""))[:700].replace(key, "[REDACTED]"), provider=self.name,
                             date_basis="unknown")
                for r in rows[:params["count"]] if isinstance(r, dict) and _url(r.get("url")) and r.get("title") and key not in r["url"]]


class TavilyKeylessProvider:
    name, auth_mode, paid, cost_usd = "tavily_keyless", "keyless", False, 0.0
    search_types = frozenset({"web", "news"})

    def __init__(self, cfg):
        self.cfg = cfg
        self.parameters = {"search_depth": cfg.get("search_depth", "advanced"), "language": "zh-cn"}

    @property
    def available(self):
        return self.cfg.get("enabled", False)

    def _send(self, endpoint, payload):
        if not self.available:
            raise SearchError("keyless_disabled", "configuration")
        req = Request("https://api.tavily.com/" + endpoint, json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json", "X-Tavily-Access-Mode": "keyless"})
        return send_json(req, timeout=self.cfg.get("request_timeout_seconds", 30), max_bytes=self.cfg.get("max_response_bytes", 2_000_000))

    def search(self, request):
        payload = dict(query=request.query, topic="news" if request.search_type == "news" else "general",
                       search_depth=self.parameters["search_depth"], max_results=min(request.max_results, 5),
                       language="zh-cn", filter_by_language=True, auto_parameters=False,
                       include_answer=False, include_raw_content=False, include_images=False, include_usage=True,
                       include_published_date=True)
        if request.domains:
            payload.update(include_domains=list(request.domains), include_domains_mode="restrict")
        if request.exclude_domains:
            payload["exclude_domains"] = list(request.exclude_domains)
        for key, value in (("start_date", request.start_date), ("end_date", request.end_date)):
            if value:
                payload[key] = value
        if request.recency_days and not request.start_date:
            payload["start_date"] = (datetime.now(timezone.utc).date() - timedelta(days=request.recency_days)).isoformat()
        data = self._send("search", payload)
        if not isinstance(data.get("results"), list):
            raise SearchError("missing_results", "invalid_response")
        results = []
        for row in data["results"][:payload["max_results"]]:
            if not isinstance(row, dict) or not row.get("title") or not _url(row.get("url")):
                continue
            stamp = row.get("published_date")
            results.append(SearchResult(str(row["title"])[:350], row["url"], str(row.get("content") or "")[:700],
                                        stamp if isinstance(stamp, str) else None, self.name))
        return results

    def extract(self, url):
        _public_http_url(url)
        # 不带query，避免供应商按相关性重排片段后冒充完整原文。
        data = self._send("extract", dict(urls=[url], extract_depth=self.cfg.get("extract_depth", "advanced"), format="text", include_usage=True))
        if not isinstance(data.get("results"), list) or not isinstance(data.get("failed_results", []), list):
            raise SearchError("invalid_extract_response", "invalid_response")
        for row in data.get("failed_results", []):
            if isinstance(row, dict) and row.get("url") == url:
                raise SearchError("extract_unavailable", "fetch_failure")
        matches = [r for r in data["results"] if isinstance(r, dict) and r.get("url", "").rstrip("/") == url.rstrip("/")]
        if len(matches) != 1 or not isinstance(matches[0].get("raw_content"), str) or not matches[0]["raw_content"].strip():
            raise SearchError("extract_unavailable", "fetch_failure")
        return matches[0]["raw_content"]


class BingNewsRssProvider:
    name, auth_mode, paid, cost_usd = "bing_news_rss", "none", False, 0.0
    search_types = frozenset({"news"})
    parameters = {"endpoint": "bing_news_rss", "date_basis": "search_result_date"}

    def __init__(self, collector):
        self.collector = collector

    @property
    def available(self):
        return True

    @staticmethod
    def endpoint(query):
        from urllib.parse import quote_plus
        return "https://www.bing.com/news/search?q=" + quote_plus(query) + "&format=rss&setlang=zh-cn"

    def search(self, request):
        query = request.query
        if request.domains:
            query += " (" + " OR ".join("site:" + d for d in request.domains) + ")"
        try:
            items = self.collector.search_query(query, limit=request.max_results, lookback_days=request.recency_days)
        except Exception:
            raise SearchError("rss_endpoint_unavailable", "invalid_response") from None
        return [SearchResult(x.title, x.url, x.summary, x.published_at or None, self.name, date_basis="search_result_date") for x in items]
