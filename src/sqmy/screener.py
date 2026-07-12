from __future__ import annotations

from difflib import SequenceMatcher
from datetime import datetime, timezone
import re
from urllib.parse import urlparse

from .models import Candidate, EventItem


TOPICS = {
    "科技创新与人工智能": ("人工智能", "算法", "算力", "机器人", "科技", "数字经济", "数据"),
    "平台经济与劳动权益": ("平台", "劳动者", "骑手", "网约车", "就业", "社保", "职业伤害"),
    "教育和未成年人": ("教育", "学校", "未成年人", "学生", "中招", "儿童"),
    "养老与医疗": ("养老", "医疗", "医院", "老年", "医保", "照护"),
    "社区治理": ("社区", "基层", "物业", "治理", "街道"),
    "营商环境与中小企业": ("中小企业", "营商", "企业", "融资", "审批", "出海"),
    "住房与公共服务": ("住房", "租赁", "公积金", "公共服务"),
    "交通与城市管理": ("交通", "停车", "城市管理", "道路"),
    "数据治理": ("个人信息", "数据治理", "数据安全", "隐私", "刷脸", "信息采集"),
    "食品安全": ("食品安全", "食品抽检", "外卖食品", "校园餐", "预制菜"),
    "青年就业": ("青年就业", "高校毕业生", "灵活就业", "见习", "招聘"),
    "国际经贸与出口": ("外贸", "出口", "跨境电商", "国际经贸", "关税", "出海"),
    "新质生产力": ("新质生产力", "成果转化", "专精特新", "产业升级"),
    "海淀科技企业和人才": ("科技企业", "科技人才", "人才引进", "研发人员", "中关村"),
}
EMPTY_PHRASES = ("会议召开", "领导调研", "表彰", "活动举行", "学习贯彻", "党建", "党史", "光辉历程", "博览会", "展览")
MECHANISM_SIGNALS = ("政策", "办法", "规定", "机制", "试点", "意见", "调查", "数据", "条例", "通知")
PROBLEM_SIGNALS = ("问题", "困难", "风险", "纠纷", "负担", "投诉", "不足", "短缺", "障碍", "争议")


def normalize_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", title.lower())


def classify(item: EventItem) -> list[str]:
    text = item.title + " " + item.summary
    return [name for name, words in TOPICS.items() if any(word in text for word in words)]


def score(item: EventItem) -> int:
    text = item.title + " " + item.summary
    value = 25 if item.region == "海淀" else 18 if item.region == "北京" else 7
    value += 18 if item.source_level == 1 else 10
    value += min(20, 6 * len(item.topics))
    if any(word in text for word in MECHANISM_SIGNALS):
        value += 15
    if any(word in text for word in PROBLEM_SIGNALS):
        value += 12
    item.timeliness_score = timeliness_score(item.published_at)
    value += item.timeliness_score
    if any(word in item.title for word in EMPTY_PHRASES):
        value -= 50
    return max(0, min(100, value))


def timeliness_score(published_at: str, reference: datetime | None = None) -> int:
    if not published_at:
        return 0
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    age = max(0, ((reference or datetime.now(timezone.utc)) - published.astimezone(timezone.utc)).days)
    if age <= 7:
        return 20
    if age <= 30:
        return 15
    if age <= 60:
        return 8
    if age <= 90:
        return 3
    return 0


def deduplicate(items: list[EventItem], threshold: float) -> list[EventItem]:
    kept, seen_urls = [], set()
    for item in sorted(items, key=lambda x: (x.published_at, -x.source_level), reverse=True):
        domain_path = urlparse(item.url).netloc + urlparse(item.url).path
        if domain_path in seen_urls:
            continue
        title = normalize_title(item.title)
        if any(SequenceMatcher(None, title, normalize_title(old.title)).ratio() >= threshold for old in kept):
            continue
        seen_urls.add(domain_path)
        kept.append(item)
    return kept


def rule_screen(items: list[EventItem], settings: dict) -> list[EventItem]:
    for item in items:
        item.topics = classify(item)
        item.rule_score = score(item)
    unique = deduplicate(items, settings["title_similarity_threshold"])
    relevant = [
        x for x in unique
        if x.topics and x.rule_score >= 35
        and not any(word in x.title for word in EMPTY_PHRASES)
        and any(word in x.title + " " + x.summary for word in MECHANISM_SIGNALS + PROBLEM_SIGNALS)
    ]
    return sorted(relevant, key=lambda x: (x.rule_score, x.published_at), reverse=True)[: settings["initial_max"]]


def to_candidate(item: EventItem, index: int, audit=None, novelty_penalty: int = 0) -> Candidate:
    analysis = getattr(item, "model_analysis", {})
    topic = primary_topic(item)
    title = analysis.get("suggested_title") or fallback_policy_title(topic, item.title + " " + analysis.get("gap_hypothesis", ""))
    local = "海淀区可直接研究或试点" if item.region == "海淀" else "北京市可协调或试点" if item.region == "北京" else "须进一步确认北京或海淀落点"
    adjusted_score = max(0, item.rule_score - novelty_penalty)
    return Candidate(
        id=f"C{index}", title=title, summary=(item.summary or item.title)[:500], event_date=item.published_at[:10], region=item.region,
        affected_group=analysis.get("affected_group", f"与{topic}相关的居民、劳动者或企业，需预研确认具体范围"),
        institutional_conflict=analysis.get("institutional_issue", "公开信息提示可能存在执行口径、信息不对称或责任衔接问题，需人工复核"),
        pain_point=analysis.get("pain_point", "真实痛点需通过调查、投诉或研究资料核验"),
        policy_gap=analysis.get("policy_gap", "需核对现行政策是否已完整覆盖"),
        policy_entry=analysis.get("policy_entry", "优先寻找流程、纠错、核验或激励约束机制"),
        authority=analysis.get("authority", local),
        data_sufficiency=analysis.get("data_assessment", "已有一条真实公开来源；核心数字尚未交叉验证"),
        policy_window=analysis.get("policy_window", "近期信息，存在初步窗口"),
        history_relation="已执行标题和历史库轻量匹配", priority="高" if adjusted_score >= 70 else "中",
        risk=analysis.get("risk", "自动初筛结果，必须人工复核；不得直接用于报送"),
        recommendation=analysis.get("recommendation", "建议进入有限预研" if adjusted_score >= 60 else "建议保留观察"), score=adjusted_score,
        gap_hypothesis=(audit.gap_hypothesis if audit else analysis.get("gap_hypothesis", analysis.get("policy_gap", ""))),
        gap_type=(audit.gap_type if audit else analysis.get("gap_type", "unclear")),
        coverage_status=(audit.coverage_status if audit else "unchecked"),
        counterevidence=(audit.counterevidence + audit.policy_matches if audit else []),
        novelty_decision=(audit.decision if audit else "proceed"),
        reframe_suggestion=("已有机制可能覆盖原假设；只有发现可验证的执行偏差时才建议改写" if audit and audit.coverage_status == "likely_covered" else ""),
        score_reasons={"规则初筛分": item.rule_score, "时效性得分": getattr(item, "timeliness_score", 0), "制度覆盖扣分": novelty_penalty, "来源名称": item.source_name, "来源等级": item.source_level, "主题": item.topics, "来源URL": item.url},
    )


def primary_topic(item: EventItem) -> str:
    text = item.title + " " + item.summary
    if any(word in text for word in ("贴息", "中小微", "融资", "营商")):
        return "营商环境与中小企业"
    if any(word in text for word in ("骑手", "职业伤害", "新就业形态")):
        return "平台经济与劳动权益"
    if "未成年人" in text:
        return "教育和未成年人"
    return item.topics[0] if item.topics else "社会治理"


def fallback_policy_title(topic: str, text: str = "") -> str:
    if "贴息" in text:
        return "关于完善北京市中小企业贴息政策兑现机制的建议"
    if "职业伤害" in text:
        return "关于完善北京市新就业形态职业伤害保障衔接机制的建议"
    if "未成年人" in text and "网络" in text:
        return "关于完善北京市未成年人网络纠纷前端治理机制的建议"
    labels = {
        "教育和未成年人": "北京市未成年人网络保护执行机制",
        "平台经济与劳动权益": "北京市新就业形态劳动者权益协同保障机制",
        "营商环境与中小企业": "北京市中小企业政策兑现机制",
    }
    subject = labels.get(topic, f"北京市{topic}相关治理机制")
    return f"关于完善{subject}的建议"
