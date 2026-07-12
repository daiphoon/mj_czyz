from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import tomllib
from urllib.parse import parse_qs, quote_plus, urlparse, urlunparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .models import EventItem


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return "" if child is None or child.text is None else child.text.strip()


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def canonical_url(value: str) -> str:
    parsed = urlparse(value)
    if "bing.com" in parsed.netloc and parsed.path.endswith("/apiclick.aspx"):
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target:
            parsed = urlparse(target)
    clean_query = "&".join(
        part for part in parsed.query.split("&")
        if part and not part.lower().startswith(("utm_", "spm=", "from="))
    )
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
    official = ("court.gov.cn", "jcy.gov.cn", "bjcourt.gov.cn", "bj148.org", "bjjubao.org.cn")
    if any(domain == item or domain.endswith("." + item) for item in official):
        return 1
    authoritative = ("news.cn", "people.com.cn", "ce.cn", "youth.cn", "chinanews.com.cn")
    if any(domain == item or domain.endswith("." + item) for item in authoritative):
        return 2
    return max(2, configured)


def infer_event_region(title: str, summary: str, url: str, source_region: str) -> tuple[str, str]:
    domain = urlparse(url).netloc.lower()
    if domain.endswith("bjhd.gov.cn"):
        return "海淀", "trusted_domain:bjhd.gov.cn"
    if domain.endswith("beijing.gov.cn") or domain.endswith("bjcourt.gov.cn"):
        return "北京", f"trusted_domain:{domain}"
    text = title + " " + summary
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

    def collect(self, run_id: str, *, fixture: Path | None = None) -> list[EventItem]:
        if fixture:
            return self._items_from_payloads(json.loads(fixture.read_text(encoding="utf-8")))
        payloads = []
        for source in self.sources:
            try:
                payloads.append({"source": source, "xml": self._fetch(source)})
            except Exception as exc:
                payloads.append({"source": source, "error": f"{type(exc).__name__}: {exc}"})
        audit = self.root / "data/runs" / run_id / "collection_audit.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(audit, [{"source": p["source"], "error": p.get("error")} for p in payloads])
        return self._items_from_payloads(payloads)

    def _fetch(self, source: dict) -> str:
        return self._fetch_query(source["query"], "feeds")

    def _fetch_query(self, query: str, namespace: str) -> str:
        url = "https://www.bing.com/news/search?q=" + quote_plus(query) + "&format=rss&setlang=zh-cn"
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        cache = self.root / "data/cache" / namespace / f"{cache_key}.xml"
        if cache.exists() and datetime.now().timestamp() - cache.stat().st_mtime < 6 * 3600:
            return cache.read_text(encoding="utf-8")
        request = Request(url, headers={"User-Agent": self.cfg["user_agent"], "Accept": "application/rss+xml, application/xml"})
        with urlopen(request, timeout=self.cfg["request_timeout_seconds"], context=ssl.create_default_context()) as response:
            raw = response.read(2_000_000)
        text = raw.decode("utf-8", errors="replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp = cache.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, cache)
        return text

    def search_query(self, query: str, *, limit: int = 3) -> list[EventItem]:
        """Run one cached metadata-only counterevidence search."""
        source = {
            "id": "novelty_counterevidence", "name": "制度新意反证检索",
            "level": 2, "region": "全国", "type": "rss_search", "query": query,
        }
        payload = {"source": source, "xml": self._fetch_query(query, "counterevidence")}
        return self._items_from_payloads([payload])[:limit]

    def _items_from_payloads(self, payloads: list[dict]) -> list[EventItem]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg["lookback_days"])
        items = []
        for payload in payloads:
            if not payload.get("xml"):
                continue
            source = payload["source"]
            try:
                root = ET.fromstring(payload["xml"])
            except ET.ParseError:
                continue
            for node in root.findall(".//item"):
                published = parse_date(_text(node, "pubDate"))
                if published and published < cutoff:
                    continue
                title = _clean_html(_text(node, "title"))
                url = canonical_url(_text(node, "link"))
                if not title or not url:
                    continue
                digest = hashlib.sha256((title + "\n" + url).encode()).hexdigest()
                summary = _clean_html(_text(node, "description"))[:700]
                event_region, region_evidence = infer_event_region(title, summary, url, source["region"])
                items.append(EventItem(
                    id=digest[:16], source_id=source["id"], source_name=source["name"], source_level=infer_source_level(url, int(source["level"])),
                    title=title, url=url, published_at=published.isoformat() if published else "",
                    summary=summary, region=event_region, source_region=source["region"], region_evidence=region_evidence,
                    collected_at=datetime.now(timezone.utc).isoformat(),
                ))
        return items

    @staticmethod
    def _atomic_json(path: Path, data: object) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
