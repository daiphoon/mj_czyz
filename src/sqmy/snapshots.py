from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .config import Settings


SNAPSHOT_REASONS = {
    "dynamic_content",
    "unstable_url",
    "critical_data",
    "likely_followup",
    "disputed_content",
}


def _public_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("快照来源必须是公开HTTP(S)地址")
    if parsed.username or parsed.password:
        raise ValueError("快照URL不得包含登录凭证")
    sensitive_keys = {
        "access_token", "api_key", "apikey", "auth", "authorization",
        "cookie", "password", "secret", "session", "sessionid", "token",
    }
    if {key.lower() for key in parse_qs(parsed.query)} & sensitive_keys:
        raise ValueError("快照URL不得包含敏感查询参数")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("不得抓取本机或局域网页面")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("不得抓取非公网IP地址")
    return value


def _safe_component(value: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-.")
    return clean[:80] or "snapshot"


def _validate_content(data: bytes, suffix: str) -> str:
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("扩展名为PDF，但文件内容不是PDF")
        return "application/pdf"
    if suffix in {".html", ".htm"}:
        prefix = data[:4096].lower()
        if not any(marker in prefix for marker in (b"<html", b"<!doctype html", b"<body")):
            raise ValueError("扩展名为HTML，但文件内容不是可识别网页")
        return "text/html"
    raise ValueError("证据快照只接受PDF或HTML")


def _load_package(path: Path) -> dict:
    raw = path.expanduser().resolve().read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("topic_id"):
        raise ValueError("证据包缺少topic_id")
    if not isinstance(payload.get("sources"), list):
        raise ValueError("证据包缺少sources")
    return payload


def capture_evidence_snapshot(
    settings: Settings,
    package_path: Path,
    source_key: str,
    *,
    reason: str,
    local_file: Path | None = None,
) -> dict:
    """仅固化人工点名的正式证据来源；不批量抓取、不调用模型。"""
    if reason not in SNAPSHOT_REASONS:
        raise ValueError(f"不支持的快照理由：{reason}")
    payload = _load_package(package_path)
    matching = [item for item in payload["sources"] if item.get("key") == source_key]
    if len(matching) != 1:
        raise ValueError("证据包中必须恰好存在一个同名source key")
    source = matching[0]
    if not source.get("used_at"):
        raise ValueError("只允许固化已标明正文用途used_at的核心来源")
    url = _public_http_url(str(source.get("url") or ""))
    cfg = settings.section("snapshots")
    max_bytes = int(cfg["max_bytes"])
    if max_bytes <= 0:
        raise ValueError("快照大小上限必须为正数")

    final_url = url
    if local_file is not None:
        input_path = local_file.expanduser().resolve()
        if not input_path.is_file():
            raise ValueError(f"本地快照文件不存在：{input_path}")
        suffix = input_path.suffix.lower()
        if input_path.stat().st_size > max_bytes:
            raise ValueError("快照文件超过配置大小上限")
        data = input_path.read_bytes()
        media_type = _validate_content(data, suffix)
    else:
        request = Request(
            url,
            headers={
                "User-Agent": settings.section("discovery")["user_agent"],
                "Accept": "text/html,application/pdf",
            },
        )
        with urlopen(
            request,
            timeout=int(cfg["request_timeout_seconds"]),
        ) as response:
            final_url = _public_http_url(response.geturl())
            content_type = response.headers.get_content_type().lower()
            if content_type == "application/pdf":
                suffix = ".pdf"
            elif content_type in {"text/html", "application/xhtml+xml"}:
                suffix = ".html"
            else:
                raise ValueError(f"不支持的网页内容类型：{content_type}")
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("在线快照超过配置大小上限")
        media_type = _validate_content(data, suffix)

    digest = hashlib.sha256(data).hexdigest()
    topic_id = str(payload["topic_id"])
    directory = (
        settings.root
        / "data/sources/snapshots"
        / _safe_component(topic_id)
    )
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / f"{_safe_component(source_key)}-{digest[:12]}{suffix}"
    if not snapshot.exists():
        temporary = snapshot.with_suffix(snapshot.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, snapshot)

    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    else:
        manifest = {}
    entries = manifest.get("snapshots")
    if not isinstance(entries, list):
        entries = []
    existing = next(
        (
            item for item in entries
            if item.get("source_key") == source_key
            and item.get("content_hash") == digest
        ),
        None,
    )
    if existing is None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        existing = {
            "source_key": source_key,
            "source_name": source.get("source_name"),
            "page_title": source.get("page_title"),
            "url": url,
            "final_url": final_url,
            "reason": reason,
            "fetched_at": fetched_at,
            "excerpt": source.get("excerpt"),
            "used_at": source.get("used_at"),
            "content_hash": digest,
            "media_type": media_type,
            "size_bytes": len(data),
            "file_path": str(snapshot.relative_to(settings.root)),
        }
        entries.append(existing)
        manifest = {
            "topic_id": topic_id,
            "notice": "仅保存正式研究实际使用且易变化的核心公开来源；不含Cookie或登录凭证。",
            "snapshots": entries,
        }
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, manifest_path)
    return {"manifest": str(manifest_path), "snapshot": existing, "model_calls": 0}
