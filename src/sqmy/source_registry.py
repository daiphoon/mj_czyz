"""来源属性与行政范围来自配置；域名权威性不等于主张已核验。"""
from pathlib import Path
import re
import tomllib
from urllib.parse import urlparse


class SourceRegistry:
    def __init__(self, path):
        path = Path(path)
        self.sources = tomllib.loads(path.read_text()).get("sources", []) if path.exists() else []
        domains = []
        for row in self.sources:
            domain = row.get("domain", "")
            if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", domain) or row.get("authority_level") not in {1, 2, 3}:
                raise ValueError("来源Registry域名或等级无效")
            if domain in domains:
                raise ValueError("来源Registry域名重复")
            domains.append(domain)

    def lookup(self, url):
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        matches = [r for r in self.sources if host == r["domain"] or host.endswith("." + r["domain"])]
        return dict(max(matches, key=lambda r: len(r["domain"]))) if matches else {
            "domain": host, "category": "other", "authority_level": 3,
            "location": "unknown", "topics": [], "primary_source": False,
        }

    def domains(self, *categories):
        return tuple(row["domain"] for row in self.sources if row.get("category") in categories)
