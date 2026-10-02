"""按能力和授权路由；熔断只影响对应搜索入口，不影响已知URL获取。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
from pathlib import Path
import threading
import time

from .collector import canonical_url
from .search_types import SearchError, SearchResponse, SearchResult, domain_matches
from .tavily import atomic_json
from .transport_audit import TransportAudit, observing


def validate_config(settings):
    cfg = settings.raw.get("search", {})
    if not cfg.get("enabled", False):
        return
    for key in ("enabled", "allow_paid", "keyless_enabled", "allow_parallel"):
        if type(cfg.get(key)) is not bool:
            raise ValueError("search开关须为布尔值")
    for key, cap in (("max_calls_per_action", 6), ("max_searches_per_action", 4), ("max_extracts_per_action", 2),
                     ("max_parallel", 2), ("failure_threshold", 10), ("cooldown_seconds", 86400)):
        value = cfg.get(key)
        if type(value) is not int or not 1 <= value <= cap:
            raise ValueError("search单行为、并行或熔断范围无效")
    if type(cfg.get("max_retries")) is not int or not 0 <= cfg["max_retries"] <= 2:
        raise ValueError("search重试上限无效")
    known = {"brave", "tavily_keyless", "bing_news_rss"}
    routes = [cfg.get("providers", [])] + list(cfg.get("routes", {}).values())
    if any(not isinstance(route, list) or not route or len(route) != len(set(route)) or set(route) - known for route in routes):
        raise ValueError("search路由须包含唯一已知入口")
    from .search_types import RetrievalIntent
    if set(cfg.get("routes", {})) - set(RetrievalIntent):
        raise ValueError("search路由用途无效")
    ttls = cfg.get("cache_ttl_hours", {})
    if set(ttls) - set(RetrievalIntent) or any(type(v) not in {int, float} or not math.isfinite(v) or not 0 < v <= 720 for v in ttls.values()):
        raise ValueError("search缓存期限无效")
    fetch = settings.raw.get("fetch", {})
    for key in ("request_timeout_seconds", "max_response_bytes", "max_text_chars", "excerpt_chars", "context_chars", "cache_ttl_hours"):
        if type(fetch.get(key)) is not int or fetch[key] <= 0:
            raise ValueError("Fetch容量和超时须为正整数")
    if type(fetch.get("allow_keyless_extract")) is not bool:
        raise ValueError("Fetch免费提取开关无效")


@dataclass
class Circuit:
    failures: int = 0
    open_until: float = 0.0
    blocked: bool = False
    probe_running: bool = False

    def allow(self, stamp):
        if self.blocked or self.open_until > stamp or self.probe_running:
            return False
        if self.open_until:
            self.probe_running = True
        return True

    def state(self, stamp):
        return "OPEN" if self.blocked or self.open_until > stamp else "HALF_OPEN" if self.open_until else "CLOSED"

    def success(self):
        self.failures, self.open_until, self.blocked, self.probe_running = 0, 0, False, False

    def failure(self, error, stamp, threshold, cooldown):
        was_probe = self.probe_running
        self.probe_running = False
        if error.category in {"invalid_query", "budget", "configuration", "local"}:
            return
        if error.category in {"authentication", "retired", "quota"}:
            self.blocked = True
            return
        self.failures += 1
        if was_probe or self.failures >= threshold or error.category in {"rate_limit", "quota", "invalid_response", "endpoint_unavailable"}:
            delay = error.retry_after if error.retry_after is not None else cooldown
            self.open_until = stamp + max(1, delay)


class SearchRouter:
    def __init__(self, providers, *, cfg=None, ledger=None, cache_dir=None, clock=time.time, sleeper=time.sleep):
        self.providers = {p.name: p for p in providers}
        self.cfg, self.ledger, self.cache_dir = cfg or {}, ledger, Path(cache_dir) if cache_dir else None
        self.clock, self.sleeper = clock, sleeper
        self.health, self._mutex = {}, threading.RLock()
        if self.cache_dir and (self.cache_dir / "health.json").exists():
            try:
                state = json.loads((self.cache_dir / "health.json").read_text())
                self.health = {name: Circuit(int(v["failures"]), float(v["open_until"]), bool(v["blocked"])) for name, v in state.items()}
            except (ValueError, KeyError, TypeError):
                self.health = {}

    def _health_key(self, provider, request):
        return provider.name + ":" + provider.auth_mode + ":" + request.search_type

    def _save_health(self, key):
        if self.cache_dir:
            path = self.cache_dir / "health.json"
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with (self.cache_dir / ".health.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    state = json.loads(path.read_text()) if path.exists() else {}
                    if not isinstance(state, dict):
                        state = {}
                except (ValueError, OSError):
                    state = {}
                c = self.health[key]
                state[key] = dict(failures=c.failures, open_until=c.open_until, blocked=c.blocked)
                atomic_json(path, state)

    def search(self, request, *, strategy="fallback", retry_failed=False, force_refresh=False):
        if strategy not in {"fallback", "parallel"}:
            raise ValueError("搜索策略无效")
        if strategy == "parallel" and not self.cfg.get("allow_parallel", False):
            raise ValueError("并行搜索未启用")
        if self.ledger:
            directory = self.ledger.s.root / "data/runs" / self.ledger.run_id
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / (".search-" + self.ledger.action.replace(":", "-") + ".lock")).open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return SearchResponse(errors=[{"code": "action_busy"}])
                return self._search(request, strategy, retry_failed, force_refresh)
        return self._search(request, strategy, retry_failed, force_refresh)

    def _search(self, request, strategy, retry_failed, force_refresh):
        order = self.cfg.get("routes", {}).get(request.intent.value, self.cfg.get("providers", list(self.providers)))
        eligible, errors = [], []
        for name in order:
            p = self.providers.get(name)
            if p is None or not p.available or p.paid and not self.cfg.get("allow_paid", False):
                errors.append({"provider": name, "code": "disabled_or_unapproved"})
            elif request.search_type not in p.search_types:
                errors.append({"provider": name, "code": "unsupported_capability"})
            else:
                eligible.append(p)
        response = SearchResponse(errors=errors)
        if strategy == "parallel":
            with ThreadPoolExecutor(max_workers=min(2, self.cfg.get("max_parallel", 2))) as pool:
                futures = [pool.submit(self._attempt, p, request, retry_failed, force_refresh, None) for p in eligible[:2]]
                parts = [f.result() for f in futures]
        else:
            parts = []
            for p in eligible:
                part = self._attempt(p, request, retry_failed, force_refresh, parts[-1][2].get("provider") if parts else None)
                parts.append(part)
                if part[0] is not None:  # 合法空结果也是成功，不继续扩搜凑数。
                    break
        seen, any_success = {}, False
        for results, error, attempt in parts:
            response.attempts.append(attempt)
            if error:
                response.errors.append({"provider": attempt["provider"], "code": error.code, "category": error.category})
            if results is not None:
                any_success = True
                for item in results:
                    url = canonical_url(item.url)
                    if request.domains and not domain_matches(url, request.domains) or domain_matches(url, request.exclude_domains):
                        continue
                    seen.setdefault(url, SearchResult(**(asdict(item) | {"url": url})))
        response.results = list(seen.values())[:request.max_results]
        response.status = "completed" if any_success else "unavailable"
        if strategy == "parallel" and any_success and any(error for _, error, _ in parts):
            response.status = "partial"
        return response

    def _attempt(self, provider, request, retry_failed, force_refresh, fallback_from):
        try:
            with self._provider_guard(self._health_key(provider, request)):
                return self._attempt_locked(provider, request, retry_failed, force_refresh, fallback_from)
        except SearchError as error:
            return None, error, dict(provider=provider.name, auth_mode=provider.auth_mode)

    @contextmanager
    def _provider_guard(self, key):
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(key.encode()).hexdigest()[:16]
            with (self.cache_dir / (".provider-" + name + ".lock")).open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SearchError("provider_busy", "circuit") from None
                # 同入口跨运行只允许一个探测；等待期间不占请求额。
                try:
                    value = json.loads((self.cache_dir / "health.json").read_text()).get(key)
                    if value:
                        self.health[key] = Circuit(int(value["failures"]), float(value["open_until"]), bool(value["blocked"]))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    pass
                yield
        else:
            yield

    def invoke_extract(self, provider, operation):
        """仅保护已知URL的Extract，不执行Search或切换供应商。"""
        if not provider.available or provider.paid and not self.cfg.get("allow_paid", False):
            raise SearchError("disabled_or_unapproved", "configuration")
        key = provider.name + ":" + provider.auth_mode + ":extract"
        with self._provider_guard(key):
            with self._mutex:
                circuit = self.health.setdefault(key, Circuit())
                if not circuit.allow(self.clock()):
                    raise SearchError("circuit_open", "circuit")
            try:
                result = operation()  # 账本占额必须在此回调中，熔断拒绝不占请求额。
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                error = exc if isinstance(exc, SearchError) else SearchError("extract_failed", "transient")
                with self._mutex:
                    circuit.failure(error, self.clock(), self.cfg.get("failure_threshold", 2), self.cfg.get("cooldown_seconds", 300))
                    self._save_health(key)
                raise error from None
            with self._mutex:
                circuit.success()
                self._save_health(key)
            return result

    def _attempt_locked(self, provider, request, retry_failed, force_refresh, fallback_from):
        digest = request.key(provider.name, provider.auth_mode, provider.parameters)
        attempt = dict(provider=provider.name, auth_mode=provider.auth_mode, intent=request.intent.value,
                       query_id=digest[:16], cache_hit=False, retry_count=0, fallback_from=fallback_from)
        cached = self._cached(digest, request) if not force_refresh else None
        previous = self.ledger.previous(digest) if self.ledger else None
        if previous and previous["status"] == "completed":
            cached = json.loads(previous["result_json"])
        elif previous and not retry_failed:
            return None, SearchError("previous_request_unresolved", "unknown_usage"), attempt
        if cached is not None:
            if self.ledger and not (previous and previous["status"] == "completed"):
                try:
                    self.ledger.reserve(provider, request, digest, cached=cached, fallback_from=fallback_from)
                except SearchError as exc:
                    return None, exc, attempt
            attempt["cache_hit"] = True
            attempt["transport"] = TransportAudit(cache=True).snapshot()
            return [SearchResult(**r) for r in cached["results"]], None, attempt
        for number in range(self.cfg.get("max_retries", 0) + 1):
            with self._mutex:
                circuit = self.health.setdefault(self._health_key(provider, request), Circuit())
                if not circuit.allow(self.clock()):
                    return None, SearchError("circuit_open", "circuit"), attempt
            call_id = None
            audit = TransportAudit(tracked=getattr(provider, "transport_audited", False))
            try:
                if self.ledger:
                    call_id = self.ledger.reserve(provider, request, digest, retry_count=number, fallback_from=fallback_from)
                started = self.clock()
                if self.ledger:
                    audit.checkpoint = lambda value: self.ledger.record_transport(call_id, value)
                with observing(audit):
                    results = provider.search(request)
                    if not isinstance(results, list) or any(not isinstance(r, SearchResult) for r in results):
                        raise SearchError("invalid_normalized_result", "invalid_response")
                audit.phase = "completed"
                attempt.update(retry_count=number, latency_ms=max(0, int((self.clock() - started) * 1000)), result_count=len(results), status="completed")
                attempt["transport"] = audit.snapshot()
                if self.ledger:
                    self.ledger.finish(call_id, results=results, latency_ms=attempt["latency_ms"], transport=audit.snapshot())
                with self._mutex:
                    circuit.success()
                    self._save_health(self._health_key(provider, request))
                if self.cache_dir:
                    atomic_json(self.cache_dir / (digest + ".json"), dict(saved_at=self.clock(), results=[asdict(r) for r in results]))
                return results, None, attempt
            except KeyboardInterrupt:
                # 账本中的running预留保留，恢复须显式决定是否重试。
                raise
            except Exception as exc:
                error = exc if isinstance(exc, SearchError) else SearchError("local_before_dispatch", "local") if audit.coverage == "tracked_http" and audit.dispatches == 0 else SearchError("provider_failed", "transient")
                audit.failure_stage = audit.failure_stage or audit.phase
                attempt["transport"] = audit.snapshot()
                if call_id is not None:
                    self.ledger.finish(call_id, error=error, transport=audit.snapshot())
                with self._mutex:
                    circuit.failure(error, self.clock(), self.cfg.get("failure_threshold", 2), self.cfg.get("cooldown_seconds", 300))
                    self._save_health(self._health_key(provider, request))
                attempt.update(status="failed", retry_count=number)
                if error.category != "transient" or number >= self.cfg.get("max_retries", 0):
                    return None, error, attempt
                self.sleeper(min(2 ** number, 5))
        return None, SearchError("provider_failed"), attempt

    def _cached(self, digest, request):
        if not self.cache_dir:
            return None
        path = self.cache_dir / (digest + ".json")
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            ttl = self.cfg.get("cache_ttl_hours", {}).get(request.intent.value, 6)
            if 0 <= self.clock() - value["saved_at"] < ttl * 3600 and isinstance(value["results"], list):
                return value
        except (ValueError, KeyError, TypeError):
            pass
        return None


def build_search_router(settings, db, run_id, action, *, collector=None):
    from .collector import SourceCollector
    from .retrieval_ledger import RetrievalLedger
    from .search_providers import BingNewsRssProvider, BraveSearchProvider, TavilyKeylessProvider
    cfg = settings.raw.get("search", {})
    validate_config(settings)
    providers = [BraveSearchProvider(settings.raw.get("brave", {}), allow_paid=cfg.get("allow_paid", False)),
                 TavilyKeylessProvider(dict(settings.raw.get("tavily", {}), enabled=cfg.get("keyless_enabled", False))),
                 BingNewsRssProvider(collector or SourceCollector(settings.root, settings.raw))]
    return SearchRouter(providers, cfg=cfg, ledger=RetrievalLedger(settings, db, run_id, action), cache_dir=settings.root / "data/cache/search")
