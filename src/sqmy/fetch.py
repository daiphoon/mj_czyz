"""已知公开URL的有界获取；不调用Search、不自动认证、不自动核验事实。"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .materials import select_excerpt
from .search_types import RetrievalIntent, SearchError, SearchRequest
from .snapshots import _public_http_url
from .tavily import atomic_json
from . import transport_audit


class DestinationRefused(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("来源目的地安全校验拒绝：" + reason)


def validate_destination(url, resolver=socket.getaddrinfo):
    from urllib.parse import urlparse
    try:
        _public_http_url(url)
    except ValueError:
        raise DestinationRefused("unsafe_url") from None
    parsed = urlparse(url)
    addresses = resolver(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses:
        raise DestinationRefused("no_dns_addresses")
    if any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):
        raise DestinationRefused("non_global_dns_address")
    return url


class _PublicRedirect(HTTPRedirectHandler):
    def __init__(self, resolver):
        self.resolver = resolver
        super().__init__()
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        transport_audit.response(code)
        transport_audit.phase("redirect_destination_validation")
        validate_destination(urljoin(req.full_url, newurl), self.resolver)
        request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if request is not None:
            transport_audit.before_dispatch()
        return request


class _Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.title, self.hidden, self.in_title = [], [], 0, False
        self.published_at = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.text.append("\n")
        if tag == "meta" and (attrs.get("property") or attrs.get("name", "")).lower() in {
            "article:published_time", "date", "pubdate", "publishdate", "publication_date"
        }:
            self.published_at = attrs.get("content")
        if tag == "time" and attrs.get("datetime") and self.published_at is None:
            self.published_at = attrs["datetime"]
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False
    def handle_data(self, value):
        if not self.hidden:
            self.text.append(value)
            if self.in_title:
                self.title.append(value)


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: str
    retrieved_at: str
    content_type: str = "unknown"
    title: str = ""
    published_at: str | None = None
    date_basis: str = "unknown"
    clean_text: str = ""
    content_hash: str | None = None
    http_status: int | None = None
    provider: str = "direct_http"
    truncated: bool = False
    error_code: str | None = None
    verification_status: str = "unverified"
    content_hash_kind: str = "response_bytes"
    transport_metadata: dict | None = None
    destination_error: str | None = None

    def record(self, terms, cfg):
        value = asdict(self)
        value.pop("clean_text")
        excerpt = select_excerpt(self.clean_text, terms, cfg) if self.clean_text else {
            "passages": [], "target_found": False, "warnings": ["未取得可核验正文"]}
        value.update(excerpt)
        value["excerpt"] = "\n".join(p["text"] for p in excerpt["passages"])
        value["excerpt_sha256"] = hashlib.sha256(value["excerpt"].encode()).hexdigest()
        value["fetch_status"] = "source_unread"  # 获取、摘录与人工内容核验是不同动作。
        value["needs_review"] = True
        return value


class DirectFetcher:
    def __init__(self, cfg, *, opener=None, resolver=socket.getaddrinfo, extractor=None, ledger=None, cache_dir=None):
        self.cfg, self.resolver = cfg, resolver
        self.opener = opener or build_opener(_PublicRedirect(resolver))
        self.extractor, self.ledger, self.cache_dir = extractor, ledger, cache_dir
        self.extract_health = None
        if extractor and ledger:
            from .search_router import SearchRouter
            self.extract_health = SearchRouter([extractor], cfg=ledger.cfg, cache_dir=ledger.s.root / "data/cache/search")

    def fetch(self, url):
        stamp = datetime.now(timezone.utc).isoformat()
        _public_http_url(url)  # 无效或含凭据的输入在任何副作用前拒绝。
        audit = transport_audit.TransportAudit(tracked=True)
        try:
            with transport_audit.observing(audit):
                return self._fetch(url, stamp, audit)
        except DestinationRefused as exc:
            return FetchResult(url, url, "refused", stamp, error_code="unsafe_destination",
                               transport_metadata=audit.snapshot(), destination_error=exc.reason)
        except HTTPError as exc:
            audit.response(exc.code)
            audit.phase = audit.failure_stage = "http_status"
            return FetchResult(url, url, "failed", stamp, http_status=exc.code,
                               error_code="http_or_parse_failed", transport_metadata=audit.snapshot())
        except Exception:
            return FetchResult(url, url, "failed", stamp, error_code="http_or_parse_failed",
                               transport_metadata=audit.snapshot())

    def _fetch(self, url, stamp, audit):
        transport_audit.phase("destination_validation")
        validate_destination(url, self.resolver)
        transport_audit.phase("request_preparation")
        request = Request(url, headers={"User-Agent": "sqmy-research/0.3", "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf"})
        open_request = self.opener.open
        transport_audit.before_dispatch()
        with open_request(request, timeout=self.cfg.get("request_timeout_seconds", 20)) as response:
            transport_audit.response(response.status)
            transport_audit.phase("final_destination_validation")
            final = validate_destination(response.geturl(), self.resolver)
            media = response.headers.get_content_type()
            transport_audit.phase("response_read")
            raw = response.read(self.cfg.get("max_response_bytes", 2_000_000) + 1)
            status = response.status
            charset = response.headers.get_content_charset() or "utf-8"
        truncated = len(raw) > self.cfg.get("max_response_bytes", 2_000_000)
        raw = raw[:self.cfg.get("max_response_bytes", 2_000_000)]
        digest = hashlib.sha256(raw).hexdigest()
        if media not in {"text/html", "application/xhtml+xml", "text/plain"}:
            audit.phase = "content_type"
            return FetchResult(url, final, "unsupported", stamp, media, content_hash=digest, http_status=status, truncated=truncated, error_code="requires_manual_extraction", transport_metadata=audit.snapshot())
        transport_audit.phase("response_parse")
        text = raw.decode(charset, errors="replace")
        title, published = "", None
        if media != "text/plain":
            parser = _Article()
            parser.feed(text)
            title, published = "".join(parser.title).strip(), parser.published_at
            text = "".join(parser.text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        incomplete = truncated or not text or "\ufffd" in text
        audit.phase = "completed" if not incomplete else "response_parse"
        return FetchResult(url, final, "incomplete" if incomplete else "fetched", stamp, media, title[:350], published,
                           "page_metadata_unverified" if published else "unknown", text, digest, status, truncated=truncated, transport_metadata=audit.snapshot())

    def fetch_record(self, url, terms, *, force_refresh=False, retry_failed=False):
        _public_http_url(url)
        if not isinstance(terms, list) or not 1 <= len(terms) <= 8 or any(not isinstance(t, str) or not t.strip() or len(t) > 100 for t in terms):
            raise ValueError("获取页需要1—8个具体目标词")
        digest = hashlib.sha256(json.dumps(dict(version=1, url=url, terms=terms, cfg=self.cfg), sort_keys=True).encode()).hexdigest()
        cache = self.cache_dir / (digest + ".json") if self.cache_dir else None
        if cache and cache.exists() and not force_refresh:
            try:
                stored = json.loads(cache.read_text())
                age = datetime.now(timezone.utc).timestamp() - stored["saved_at"]
                if 0 <= age < self.cfg.get("cache_ttl_hours", 24) * 3600 and stored["record"].get("status") == "fetched":
                    return dict(stored["record"], cache_hit=True)
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        extract_digest = "extract:" + digest
        previous = self.ledger.previous(extract_digest) if self.ledger else None
        if previous and previous["status"] == "completed":
            return dict(json.loads(previous["result_json"])["results"][0], cache_hit=True)
        record = self.fetch(url).record(terms, self.cfg)
        if record["status"] in {"failed", "incomplete"} and self.extractor and self.cfg.get("allow_keyless_extract", False) and self.ledger:
            if previous and not retry_failed:
                record["extract_error"] = "previous_request_unresolved"
            else:
                call_id = None
                extract_audit = transport_audit.TransportAudit(tracked=getattr(self.extractor, "transport_audited", False))
                try:
                    def extract():
                        nonlocal call_id
                        request = SearchRequest("known_url:" + digest, intent=RetrievalIntent.REGULATION_SEARCH, max_results=1)
                        call_id = self.ledger.reserve(self.extractor, request, extract_digest, endpoint="extract")
                        extract_audit.checkpoint = lambda value: self.ledger.record_transport(call_id, value)
                        try:
                            with transport_audit.observing(extract_audit):
                                return self.extractor.extract(url)
                        except Exception as exc:
                            if not isinstance(exc, SearchError) and extract_audit.dispatches == 0 and extract_audit.coverage == "tracked_http":
                                raise SearchError("local_before_dispatch", "local") from None
                            raise
                    text = self.extract_health.invoke_extract(self.extractor, extract)
                    cap = self.cfg.get("max_text_chars", 100_000)
                    truncated = len(text) > cap
                    text = text[:cap]
                    direct_record = record
                    extract_audit.phase = "completed"
                    result = FetchResult(url, url, "incomplete" if truncated else "fetched", datetime.now(timezone.utc).isoformat(), direct_record["content_type"],
                                         clean_text=text, content_hash=hashlib.sha256(text.encode()).hexdigest(), provider=self.extractor.name,
                                         truncated=truncated, content_hash_kind="extract_returned_text", transport_metadata=extract_audit.snapshot())
                    record = result.record(terms, self.cfg)
                    record.update(extraction_method="tavily_keyless_extract", extracted_content_type="text/plain",
                                  direct_fetch_metadata={k: direct_record.get(k) for k in
                                      ("status", "content_type", "final_url", "http_status", "error_code", "content_hash", "content_hash_kind", "truncated", "transport_metadata")})
                    self.ledger.finish(call_id, record=record, transport=extract_audit.snapshot())
                except SearchError as exc:
                    if call_id is not None:
                        self.ledger.finish(call_id, error=exc, transport=extract_audit.snapshot())
                    record["extract_error"] = exc.code
                except KeyboardInterrupt:
                    raise
                except Exception:
                    if call_id is not None:
                        self.ledger.finish(call_id, error=SearchError("extract_failed", "transient"), transport=extract_audit.snapshot())
                    record["extract_error"] = "extract_failed"
        if cache and record["status"] == "fetched":
            atomic_json(cache, {"saved_at": datetime.now(timezone.utc).timestamp(), "record": record})
        return record
