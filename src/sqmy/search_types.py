"""供应商无关的发现契约；搜索命中不等于已经核验的证据。"""
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
import hashlib
import json
import re
from typing import Protocol
from urllib.parse import urlparse


class RetrievalIntent(StrEnum):
    HOTSPOT_DISCOVERY = "HOTSPOT_DISCOVERY"
    POLICY_SEARCH = "POLICY_SEARCH"
    REGULATION_SEARCH = "REGULATION_SEARCH"
    OFFICIAL_RESPONSE = "OFFICIAL_RESPONSE"
    CASE_SEARCH = "CASE_SEARCH"
    COUNTER_EVIDENCE = "COUNTER_EVIDENCE"
    LOCAL_PRACTICE = "LOCAL_PRACTICE"
    COMPARATIVE_CASE = "COMPARATIVE_CASE"
    IMAGE_SEARCH = "IMAGE_SEARCH"


def domain_matches(url, domains):
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


@dataclass(frozen=True)
class SearchRequest:
    query: str
    intent: RetrievalIntent = RetrievalIntent.HOTSPOT_DISCOVERY
    max_results: int = 5
    recency_days: int | None = None
    domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    search_type: str = "web"
    start_date: str | None = None
    end_date: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "intent", RetrievalIntent(self.intent))
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 500:
            raise ValueError("检索词须为500字以内的具体问题")
        if isinstance(self.max_results, bool) or not isinstance(self.max_results, int) or not 1 <= self.max_results <= 10:
            raise ValueError("搜索结果上限须为1—10")
        if self.recency_days is not None and (isinstance(self.recency_days, bool) or not isinstance(self.recency_days, int) or self.recency_days < 1):
            raise ValueError("检索时间范围无效")
        for key in ("domains", "exclude_domains"):
            values = tuple(str(d).lower().rstrip(".") for d in getattr(self, key))
            if len(values) > 30 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", d) or ".." in d or "." not in d for d in values):
                raise ValueError("域名筛选只接受公开域名")
            object.__setattr__(self, key, values)
        if self.search_type not in {"web", "news", "image"}:
            raise ValueError("搜索类型无效")
        for value in (self.start_date, self.end_date):
            if value is not None:
                date.fromisoformat(value)
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("检索开始日期晚于截止日期")

    def key(self, provider, auth_mode, parameters):
        value = dict(version=1, request=asdict(self), provider=provider, auth_mode=auth_mode, parameters=parameters)
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    provider: str = "unknown"
    source_type: str = "unclassified"
    score: float | None = None
    date_basis: str = "provider_reported_unverified"
    verification_status: str = "unverified"


@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    status: str = "unavailable"
    errors: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class SearchError(Exception):
    def __init__(self, code, category="failure", *, http_status=None, retry_after=None):
        self.code, self.category = code, category
        self.http_status, self.retry_after = http_status, retry_after
        super().__init__(code)


class SearchProvider(Protocol):
    name: str
    auth_mode: str
    paid: bool
    search_types: frozenset[str]
    parameters: dict
    cost_usd: float

    @property
    def available(self) -> bool: ...
    def search(self, request: SearchRequest) -> list[SearchResult]: ...
