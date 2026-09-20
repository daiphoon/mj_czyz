from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import ssl
import tomllib
from urllib.parse import parse_qs, parse_qsl, urlencode, quote_plus, urlparse, urlunparse, urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .models import EventItem
from .materials import material_notes, mark_reposts, split_discovery_summary
from .discovery_coverage import provenance


class _ListingItem(HTMLParser):
    """只读取目录项中的标题链接与日期，不抓取文章正文。"""
    def __init__(self):
        super().__init__()
        self.links, self.dates = [], []
        self.anchor = None
        self.in_span = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.anchor = {"url": attrs.get("href", ""), "title": attrs.get("title", ""), "text": ""}
        elif tag == "span":
            self.in_span = True

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["text"] += data
        if self.in_span:
            self.dates.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append(self.anchor)
            self.anchor = None
        elif tag == "span":
            self.in_span = False


def listing_to_rss(html: str, source: dict) -> str:
    channel = ET.Element("channel")
    seen = set()
    for block in re.findall(r"<li\b[^>]*>.*?</li>", html, flags=re.S | re.I):
        if source.get("listing_format") == "court_date":
            # 最高法目录日期明确位于 i.date；不取标题年份或URL日期。
            block = re.sub(r'<i\s+class=["\']date["\']\s*>([^<]+)</i>',
                           r'<span>\1</span>', block, flags=re.I)
        if source.get("listing_format") == "haidian_medical":
            # 只识别已核验的空 strLink 静态分支，不执行 JS 或猜测动态跳转。
            def literal_link(match):
                script = re.fullmatch(
                    r'''\s*var\s+strLink\s*=\s*'';\s*if\(!strLink\)\{document.write\('(<a href="[^"]+" target="_blank">[^<]+</a>)'\)\}\s*'''
                    r'''else\{document.write\('<a href="'\+strLink\+'" target="_blank">[^<]+</a>'\)\}\s*''',
                    match.group(1),
                )
                return script.group(1) if script else ""
            block = re.sub(r"<script\b[^>]*>(.*?)</script>", literal_link, block, flags=re.S | re.I)
        parser = _ListingItem()
        parser.feed(block)
        date_match = re.search(r"\b(\d{4})[-/](\d{2})[-/](\d{2})\b", " ".join(parser.dates))
        if not date_match:
            continue
        day = "-".join(date_match.groups())
        for link in parser.links:
            url = canonical_url(urljoin(source["url"], link["url"]))
            title = (link["title"] or link["text"]).strip()
            if not title or not url.startswith(source["item_url_prefix"]) or url in seen:
                continue
            seen.add(url)
            item = ET.SubElement(channel, "item")
            for key, value in (("title", title), ("link", url), ("pubDate", day), ("description", "")):
                ET.SubElement(item, key).text = value
            if len(seen) >= source["max_items"]:
                return ET.tostring(channel, encoding="unicode")
    if not seen:
        raise ValueError("目录未解析出带日期的文章；需检查来源结构，不自动扩大抓取")
    return ET.tostring(channel, encoding="unicode")


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return "" if child is None or child.text is None else child.text.strip()


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _parse_rss(value: str) -> ET.Element:
    """区分合法空订阅与返回首页、错误页或损坏 XML。"""
    try:
        root = ET.fromstring(value)
    except ET.ParseError as exc:
        raise ValueError("RSS 响应不是有效 XML；需检查入口或重定向") from exc
    if root.tag != "channel" and (root.tag != "rss" or root.find("channel") is None):
        raise ValueError("RSS 响应缺少 rss/channel 结构；可能返回了 HTML 首页")
    return root


def canonical_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.hostname == "bing.com" or (parsed.hostname or "").endswith(".bing.com")) and parsed.path.endswith("/apiclick.aspx"):
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target:
            parsed = urlparse(target)
    clean_query = urlencode(sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                                  if not key.lower().startswith("utm_") and key.lower() not in {"spm", "from"}))
    return urlunparse((parsed.scheme.lower() or "https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", clean_query, ""))


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None


def infer_source_level(url: str, configured: int) -> int:
    domain = urlparse(url).netloc.lower()
    if domain.endswith(".gov.cn") or domain in {"gov.cn", "www.gov.cn"}:
        return 1
    official = ("court.gov.cn", "jcy.gov.cn", "bjcourt.gov.cn", "bjjc.gov.cn", "bj148.org", "bjjubao.org.cn")
    if any(domain == item or domain.endswith("." + item) for item in official):
        return 1
    authoritative = ("news.cn", "people.com.cn", "ce.cn", "youth.cn", "chinanews.com.cn",
                     "cnr.cn", "xinhuanet.com", "workercn.cn", "legaldaily.com.cn", "cctv.com")
    if any(domain == item or domain.endswith("." + item) for item in authoritative):
        return 2
    return max(2, configured)


def infer_event_region(title: str, summary: str, url: str, source_region: str) -> tuple[str, str]:
    domain = urlparse(url).netloc.lower()
    if domain.endswith("bjhd.gov.cn"):
        return "海淀", "trusted_domain:bjhd.gov.cn"
    if domain.endswith("beijing.gov.cn") or domain.endswith("bjcourt.gov.cn") or domain.endswith("bjjc.gov.cn"):
        return "北京", f"trusted_domain:{domain}"
    text = title + " " + split_discovery_summary(summary)['reported_excerpt']
    text = re.sub(r"(?:新华网|中新网|人民网)?北京\d{1,2}月\d{1,2}日电\s*", "", text)
    if "海淀" in text:
        return "海淀", "text:haidian"
    beijing_markers = ("北京市", "北京互联网法院", "北京市委", "北京市政府", "北京市网信", "北京市市场监管")
    marker = next((item for item in beijing_markers if item in text), None)
    if marker:
        return "北京", f"text:{marker}"
    return "全国", f"source_channel_only:{source_region}"


class SourceCollector:
    def __init__(self, root: Path, settings: dict):
        self.root = root
        self.cfg = settings["discovery"]
        with (root / "config/sources.toml").open("rb") as fh:
            self.sources = tomllib.load(fh)["sources"]
        self.collection_stats: list[dict] = []
        self._rss_endpoint_error: str | None = None

    def collect(
        self,
        run_id: str,
        *,
        fixture: Path | None = None,
        clue_file: Path | None = None,
    ) -> list[EventItem]:
        if fixture:
            payloads = json.loads(fixture.read_text(encoding="utf-8"))
        else:
            self._rss_endpoint_error = None
            payloads: list[dict | None] = [None] * len(self.sources)
            workers = max(1, int(self.cfg.get("max_parallel_fetches", 6)))
            remaining = list(enumerate(self.sources))
            if self.cfg.get("rss_endpoint_circuit_breaker", False):
                # 同一入口先取得一条有效响应；首页错误不应重复发送40次。
                first = next(((i, s) for i, s in remaining if s.get('type') == 'rss_search'), None)
                if first:
                    index, source = first
                    try:
                        payloads[index] = {'source': source, 'xml': self._fetch(source)}
                    except Exception as exc:
                        payloads[index] = {'source': source, 'error': f'{type(exc).__name__}: {exc}'}
                    remaining = [(i, s) for i, s in remaining if i != index]
                if self._rss_endpoint_error:
                    for index, source in remaining:
                        if source.get('type') == 'rss_search':
                            payloads[index] = {'source': source, 'error': self._rss_endpoint_error,
                                               'request_status': 'skipped_endpoint_unavailable'}
                    remaining = [(i, s) for i, s in remaining if s.get('type') != 'rss_search']
            with ThreadPoolExecutor(max_workers=min(workers, len(self.sources) or 1)) as executor:
                futures = {
                    executor.submit(self._fetch, source): (index, source)
                    for index, source in remaining
                }
                for future in as_completed(futures):
                    index, source = futures[future]
                    try:
                        payloads[index] = {"source": source, "xml": future.result()}
                    except Exception as exc:
                        payloads[index] = {
                            "source": source,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
            payloads = [payload for payload in payloads if payload is not None]
        audit = self.root / "data/runs" / run_id / "collection_audit.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        stats: list[dict] = []
        events = self._items_from_payloads(payloads, _stats=stats)
        clue_snapshot = None
        if clue_file:
            clue_events, clue_stats, clue_snapshot = self._items_from_clue_file(
                run_id, clue_file
            )
            events.extend(clue_events)
            stats.extend(clue_stats)
        self.collection_stats = stats
        for item in events:
            if not item.material:
                item.material = material_notes(item.title, item.summary, item.published_at, source_level=item.source_level)
            item.material.setdefault('discovery_provenance', provenance(
                item.url, channel='curated_import', source_id=item.source_id,
                date_basis='curator_supplied_date'))
        mark_reposts(events)
        count_keys = (
            "fetched_count", "within_window_count", "collected_count",
            "invalid_metadata_count", "outside_window_count",
        )
        self._atomic_json(audit, {
            "run_id": run_id,
            "fixture": bool(fixture),
            "clue_snapshot": str(clue_snapshot) if clue_snapshot else None,
            "sources": stats,
            "totals": {key: sum(int(item[key]) for item in stats) for key in count_keys},
        })
        return events

    def _items_from_clue_file(
        self, run_id: str, clue_file: Path
    ) -> tuple[list[EventItem], list[dict], Path]:
        """导入由受控网页/社交检索生成的元数据线索，不保存网页全文。"""
        path = clue_file.expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise ValueError(f"线索文件不存在：{path}")
        records = []
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"线索文件第{line_number}行不是有效JSON：{exc}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(f"线索文件第{line_number}行必须是JSON对象")
            records.append((line_number, record))

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg["lookback_days"])
        items: list[EventItem] = []
        stats_by_source: dict[str, dict] = {}
        normalized: list[dict] = []
        sensitive_keys = {
            "access_token", "api_key", "apikey", "auth", "authorization",
            "cookie", "password", "secret", "session", "sessionid", "token",
        }
        required = {"title", "url", "published_at", "summary", "source_name", "source_level"}
        for line_number, record in records:
            missing = sorted(required - set(record))
            if missing:
                raise ValueError(
                    f"线索文件第{line_number}行缺少字段：{','.join(missing)}"
                )
            title = _clean_html(str(record["title"]))
            summary = _clean_html(str(record["summary"]))[:700]
            url = canonical_url(str(record["url"]))
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError(f"线索文件第{line_number}行URL必须是公开HTTP(S)地址")
            query_keys = {key.lower() for key in parse_qs(parsed_url.query)}
            if query_keys & sensitive_keys:
                raise ValueError(f"线索文件第{line_number}行URL包含敏感查询参数")
            published = parse_date(str(record["published_at"]))
            configured_level = record["source_level"]
            if isinstance(configured_level, bool) or configured_level not in {2, 3}:
                raise ValueError(f"线索文件第{line_number}行source_level只能是2或3")
            source_name = _clean_html(str(record["source_name"]))
            source_id = str(record.get("source_id") or "clue_" + hashlib.sha256(
                (parsed_url.netloc + "\n" + source_name).encode()
            ).hexdigest()[:12])
            stat = stats_by_source.setdefault(source_id, {
                "source_id": source_id,
                "source_name": source_name,
                "expansion_tier": 4,
                "fetched_count": 0,
                "within_window_count": 0,
                "collected_count": 0,
                "invalid_metadata_count": 0,
                "outside_window_count": 0,
                "fetch_error": None,
                "parse_error": None,
            })
            stat["fetched_count"] += 1
            if published and published < cutoff:
                stat["outside_window_count"] += 1
                continue
            stat["within_window_count"] += 1
            if not title or not summary:
                stat["invalid_metadata_count"] += 1
                continue
            inferred_region, inferred_evidence = infer_event_region(
                title, summary, url, str(record.get("source_region") or "全国")
            )
            declared_region = str(record.get("region") or inferred_region)
            if declared_region not in {"海淀", "北京", "全国"}:
                raise ValueError(f"线索文件第{line_number}行region只能是海淀、北京或全国")
            if declared_region in {"海淀", "北京"} and inferred_region == "全国":
                supplied_evidence = str(record.get("region_evidence") or "")
                if not supplied_evidence:
                    raise ValueError(f"线索文件第{line_number}行地方地域缺少region_evidence")
                region_evidence = "clue_import:" + supplied_evidence[:160]
            else:
                region_evidence = inferred_evidence
            digest = hashlib.sha256((title + "\n" + url).encode()).hexdigest()
            item = EventItem(
                id=digest[:16], source_id=source_id, source_name=source_name,
                source_level=infer_source_level(url, int(configured_level)),
                title=title, url=url, published_at=published.isoformat() if published else "",
                summary=summary, region=declared_region,
                source_region=str(record.get("source_region") or "全国"),
                region_evidence=region_evidence, expansion_tier=4,
                collected_at=datetime.now(timezone.utc).isoformat(),
            )
            item.material = material_notes(title, summary, item.published_at, source_level=item.source_level)
            for field in ("event_at", "updated_at"):
                value = parse_date(str(record.get(field) or ""))
                item.material[field] = value.isoformat() if value else ""
            items.append(item)
            stat["collected_count"] += 1
            normalized.append({
                "title": item.title,
                "url": item.url,
                "published_at": item.published_at,
                "summary": item.summary,
                "source_id": item.source_id,
                "source_name": item.source_name,
                "source_level": item.source_level,
                "region": item.region,
                "source_region": item.source_region,
                "region_evidence": item.region_evidence,
                "material": item.material,
            })

        snapshot = self.root / "data/runs" / run_id / "discovery_clues.jsonl"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        temp = snapshot.with_suffix(snapshot.suffix + ".tmp")
        temp.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in normalized),
            encoding="utf-8",
        )
        os.replace(temp, snapshot)
        return items, list(stats_by_source.values()), snapshot

    def _fetch(self, source: dict) -> str:
        if source.get("type") == "html_index":
            return self._fetch_url(source["url"], "indexes")
        return self._fetch_query(source["query"], "feeds")

    def _fetch_query(self, query: str, namespace: str) -> str:
        if self._rss_endpoint_error:
            raise ValueError(self._rss_endpoint_error)
        url = "https://www.bing.com/news/search?q=" + quote_plus(query) + "&format=rss&setlang=zh-cn"
        text = self._fetch_url(url, namespace)
        self._reject_search_html(text)
        return text

    def _reject_search_html(self, text: str) -> None:
        if re.match(r'\s*(?:<!doctype\s+html|<html\b)', text, flags=re.I):
            message = 'Bing新闻RSS入口返回HTML，当前采集器停止重复请求；未核验反证，不能当作空结果'
            if self.cfg.get('rss_endpoint_circuit_breaker', False):
                self._rss_endpoint_error = message
            raise ValueError(message)

    def _fetch_url(self, url: str, namespace: str) -> str:
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        cache = self.root / "data/cache" / namespace / f"{cache_key}.xml"
        cache_ttl_seconds = int(self.cfg["cache_ttl_hours"]) * 3600
        if cache.exists() and datetime.now().timestamp() - cache.stat().st_mtime < cache_ttl_seconds:
            text = cache.read_text(encoding="utf-8")
            if namespace in {"feeds", "counterevidence"}:
                self._reject_search_html(text)
                _parse_rss(text)
            return text
        request = Request(url, headers={"User-Agent": self.cfg["user_agent"], "Accept": "application/rss+xml, application/xml, text/html"})
        with urlopen(request, timeout=self.cfg["request_timeout_seconds"], context=ssl.create_default_context()) as response:
            raw = response.read(2_000_000)
        text = raw.decode("utf-8", errors="replace")
        if namespace in {"feeds", "counterevidence"}:
            self._reject_search_html(text)
            _parse_rss(text)
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp = cache.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, cache)
        return text

    def search_query(
        self,
        query: str,
        *,
        limit: int = 3,
        lookback_days: int | None = None,
    ) -> list[EventItem]:
        """Run one cached metadata-only counterevidence search."""
        source = {
            "id": "novelty_counterevidence", "name": "制度新意反证检索",
            "level": 2, "region": "全国", "type": "rss_search", "query": query,
        }
        payload = {"source": source, "xml": self._fetch_query(query, "counterevidence")}
        stats: list[dict] = []
        items = self._items_from_payloads([payload], lookback_days=lookback_days, _stats=stats)
        if stats[0]["parse_error"]:
            raise ValueError(f"反证 RSS 解析失败：{stats[0]['parse_error']}")
        return items[:limit]

    def _items_from_payloads(
        self,
        payloads: list[dict],
        *,
        lookback_days: int | None = None,
        _stats: list[dict] | None = None,
    ) -> list[EventItem]:
        window_days = self.cfg["lookback_days"] if lookback_days is None else lookback_days
        if window_days < 0:
            raise ValueError("检索回看天数不得为负数")
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        items = []
        for payload in payloads:
            source = payload["source"]
            stat = {
                "source_id": source["id"],
                "source_name": source["name"],
                "expansion_tier": int(source.get("expansion_tier", 1)),
                "fetched_count": 0,
                "within_window_count": 0,
                "collected_count": 0,
                "invalid_metadata_count": 0,
                "outside_window_count": 0,
                "fetch_error": payload.get("error"),
                "parse_error": None,
                "request_status": payload.get('request_status', 'attempted'),
            }
            if not payload.get("xml"):
                if not stat["fetch_error"]:
                    stat["parse_error"] = "ValueError: RSS 响应为空"
                if _stats is not None:
                    _stats.append(stat)
                continue
            try:
                content = listing_to_rss(payload["xml"], source) if source.get("type") == "html_index" else payload["xml"]
                root = _parse_rss(content)
            except (ET.ParseError, ValueError) as exc:
                stat["parse_error"] = f"{type(exc).__name__}: {exc}"
                if _stats is not None:
                    _stats.append(stat)
                continue
            nodes = root.findall(".//item")
            stat["fetched_count"] = len(nodes)
            for node in nodes:
                published = parse_date(_text(node, "pubDate"))
                if published and published < cutoff:
                    stat["outside_window_count"] += 1
                    continue
                stat["within_window_count"] += 1
                title = _clean_html(_text(node, "title"))
                url = canonical_url(_text(node, "link"))
                if not title or not url:
                    stat["invalid_metadata_count"] += 1
                    continue
                digest = hashlib.sha256((title + "\n" + url).encode()).hexdigest()
                summary = _clean_html(_text(node, "description"))[:700]
                event_region, region_evidence = infer_event_region(title, summary, url, source["region"])
                items.append(EventItem(
                    id=digest[:16], source_id=source["id"], source_name=source["name"], source_level=infer_source_level(url, int(source["level"])),
                    title=title, url=url, published_at=published.isoformat() if published else "",
                    summary=summary, region=event_region, source_region=source["region"], region_evidence=region_evidence,
                    expansion_tier=int(source.get("expansion_tier", 1)),
                    collected_at=datetime.now(timezone.utc).isoformat(),
                    material=material_notes(title, summary, published.isoformat() if published else "", source_level=infer_source_level(url, int(source["level"]))),
                ))
                items[-1].material['discovery_provenance'] = provenance(
                    url, channel='direct_index' if source.get('type') == 'html_index' else 'bing_news_rss',
                    source_id=source['id'], material_kind=source.get('material_kind', 'unclassified'),
                    date_basis=source.get('date_basis', 'publisher_date' if source.get('type') == 'html_index' else 'search_result_date'),
                    query_id=source['id'] if source.get('type') == 'rss_search' else None)
                if source.get('date_basis') == 'listing_displayed_date':
                    items[-1].material['date_note'] = '目录显示日期；来信、答复、发布和事件日期尚须分别回源核验，不自动等同。'
                stat["collected_count"] += 1
            if _stats is not None:
                _stats.append(stat)
        return items

    @staticmethod
    def _atomic_json(path: Path, data: object) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
