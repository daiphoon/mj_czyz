"""零模型材料标注与定向摘录；提示用途，不替代事实核验或新增准入闸门。"""
from difflib import SequenceMatcher
from datetime import datetime, timezone
import hashlib
import re


def split_discovery_summary(summary: str) -> dict[str, str]:
    """只分隔显式标注的上游设想；前半仍是待回源陈述，不自动认证事实。"""
    boundary = re.search(
        r"(?:^|(?<=[。；！？\n]))\s*(?:可核验缺口是|缺口假设(?:（待核）)?|待核假设|待核问题|原因推测|建议切口)\s*[:：]",
        summary,
    )
    index = boundary.start() if boundary else len(summary)
    return {"reported_excerpt": summary[:index], "upstream_hypotheses": summary[index:]}


def compact_reposts(events):
    """近日期长摘要高度重合时合并模型材料；保留原始事件，不视作独立核验。"""
    kept, merged = [], {}
    # 同组优先高等级来源；不改写输入事件或日期。
    for event in sorted(events, key=lambda item: item.source_level):
        match = None
        for previous in kept:
            try:
                a, b = [datetime.fromisoformat(x.published_at.replace('Z', '+00:00')) for x in (event, previous)]
                a = a.replace(tzinfo=timezone.utc) if a.tzinfo is None else a
                b = b.replace(tzinfo=timezone.utc) if b.tzinfo is None else b
                near = abs((a - b).total_seconds()) <= 86400
            except ValueError:
                near = False
            numbers = [re.findall(r'\d+(?:\.\d+)?', x.summary) for x in (event, previous)]
            if (near and min(len(event.summary), len(previous.summary)) >= 160
                    and not any(a != b for a, b in zip(*numbers))
                    and SequenceMatcher(None, event.summary, previous.summary, autojunk=False).ratio() >= 0.9):
                match = previous
                break
        if match:
            merged.setdefault(match.id, []).append(event.id)
        else:
            kept.append(event)
    order = {event.id: index for index, event in enumerate(events)}
    return sorted(kept, key=lambda event: order[event.id]), merged


def material_notes(title, summary, published_at, *, source_level=3, purpose="discovery", updated_at="", event_at=""):
    text = title + " " + summary
    if purpose == "policy":
        role = "policy_reference"
    elif any(word in title for word in ("征求意见", "意见征集")):
        role = "policy_window"
    elif source_level == 3:
        role = "unverified_clue"
    elif any(word in title for word in ("办法", "条例", "规定", "印发", "通知")):
        role = "policy_reference"
    else:
        role = "event_clue"
    promotion = any(word in text for word in ("招生", "限时优惠", "点击咨询", "课程报名", "品牌推广", "软文", "广告"))
    origin = re.search(r"(?:来源\s*[:：]|转载自)\s*([\u4e00-\u9fffA-Za-z]{2,20})", text)
    return {
        "purpose": purpose, "role_hint": role, "promotion_suspected": promotion,
        "published_at": published_at, "updated_at": updated_at, "event_at": event_at,
        "date_status": "source_reported_unverified" if published_at else "needs_review",
        "date_note": "发布时间不等于事件时间；搜索日期过滤也可能依据更新时间",
        "original_source_hint": origin.group(1) if origin else "",
        "origin_status": "unverified", "repost_group_hint": "",
    }


def mark_reposts(events):
    """只提示疑似同源，不把不同URL/不同分组当作独立证据。"""
    for index, event in enumerate(events):
        for previous in events[:index]:
            a, b = event.material, previous.material
            shared_origin = a.get("original_source_hint") and a.get("original_source_hint") == b.get("original_source_hint")
            title_match = SequenceMatcher(None, event.title, previous.title).ratio() >= 0.78
            # 长摘要高度相似是线索，不以同机构同主题直接认定同篇文章。
            text_match = min(len(event.summary), len(previous.summary)) >= 80 and SequenceMatcher(None, event.summary, previous.summary).ratio() >= 0.9
            if (shared_origin and title_match) or text_match:
                group = b.get("repost_group_hint") or hashlib.sha256(previous.url.encode()).hexdigest()[:16]
                a["repost_group_hint"] = b["repost_group_hint"] = group
                break


def select_excerpt(text, terms, cfg):
    """在完整的有界响应内找目标及限制条件，保存位置；不截取网页前N字冒充全文。"""
    cap, radius = cfg["excerpt_chars"], cfg["context_chars"]
    anchors = sorted({match.start() for term in terms for match in re.finditer(re.escape(term), text)})
    conditions = list(re.finditer(r"适用|不适用|除外|但是|不得|施行|生效|修订|版本|有效期|征求意见", text))
    selected, ranges = [], []
    # 同时保留目标、条件、版本位置；剩余命中是否遗漏会显式标注。
    positions = []
    for index in range(max(len(anchors), len(conditions))):
        if index < len(anchors):
            positions.append(anchors[index])
        if index < len(conditions):
            positions.append(conditions[index].start())
    for position in positions:
        start, end = max(0, position - radius), min(len(text), position + radius)
        merged = []
        for left, right in sorted(ranges + [(start, end)]):
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))
        if sum(right - left for left, right in merged) <= cap:
            ranges = merged
    for start, end in sorted(ranges):
        selected.append({"start": start, "end": end, "text": text[start:end]})
    versions = sorted(set(re.findall(r"(?:版本|修订|更新|施行|生效)[^\n。]{0,25}", text)))[:10]
    return {
        "passages": selected, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "content_chars": len(text), "target_found": bool(anchors), "version_hints": versions,
        "needs_review": True,
        "warnings": ["摘录不等于事实核验通过；核对适用对象、例外、版本及原文上下文"]
        + (["未找到指定目标词"] if not anchors else [])
        + (["有目标命中未进入摘录"] if any(not any(a <= p < b for a, b in ranges) for p in anchors) else [])
        + (["有条件或版本线索未进入摘录，需回看原文"] if any(not any(a <= match.start() < b for a, b in ranges) for match in conditions) else [])
        + (["存在多个版本/生效线索，不能仅据首段认定当前版本"] if len(versions) > 1 else []),
    }
