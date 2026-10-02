from __future__ import annotations

from difflib import SequenceMatcher
from datetime import datetime, timezone
import re
from .collector import canonical_url
from zoneinfo import ZoneInfo

from .models import Candidate, EventItem
from .materials import split_discovery_summary


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
    "消费者权益与市场秩序": (
        "消费者", "收费", "退费", "售后", "维权", "虚假宣传", "价格不透明", "物业费",
    ),
    "社会保障与社会救助": (
        "社会保障", "社会救助", "低保", "工伤", "失业保险", "异地结算", "异地就医",
    ),
    "妇女儿童与家庭发展": (
        "生育", "托育", "婴幼儿", "孕产妇", "家庭暴力", "妇女权益", "儿童福利",
    ),
    "残障人与无障碍权益": (
        "残障人", "残疾人", "无障碍", "轮椅", "孤独症", "辅助器具",
    ),
    "三农与乡村公共服务": (
        "三农", "农村", "农民", "农产品", "农村养老", "农民工", "乡村公共服务",
    ),
    "生态环境与公共安全": (
        "环境污染", "噪声", "消防通道", "安全生产", "灾害预警", "城市内涝", "应急救援",
    ),
    "金融与通信服务": (
        "银行服务", "保险理赔", "电信套餐", "手机号", "自动续费", "支付服务", "金融消费者",
    ),
    "政务服务与数字鸿沟": (
        "政务服务", "办事大厅", "一网通办", "证明材料", "多头证明", "老年人线上办理", "数字鸿沟",
    ),
}
EMPTY_PHRASES = (
    "会议召开", "主持召开", "领导调研", "表彰", "活动举行", "成功举办", "洽谈会",
    "招聘会", "专场招聘", "学习贯彻", "党建", "党史", "光辉历程", "博览会", "展览",
    "正式施行", "民生答卷", "总体平稳", "高质量发展之路",
)
MECHANISM_SIGNALS = ("政策", "办法", "规定", "机制", "试点", "意见", "调查", "数据", "条例", "通知")
PROBLEM_SIGNALS = (
    "问题", "困难", "风险", "纠纷", "负担", "投诉", "不足", "短缺", "障碍", "争议",
    "困境", "维权", "拖欠", "乱象", "陷阱", "门槛", "多头证明", "申诉无门", "申诉不畅",
    "不透明", "不便", "失灵", "滞后", "缺口", "不畅", "误伤", "转嫁成本", "反复提交",
)
EVIDENCE_SIGNALS = (
    "调查", "数据", "统计", "通报", "抽检", "审计", "判决", "案件", "案例", "处罚",
    "投诉", "举报", "征求意见", "执法", "曝光", "追踪", "监测", "事故", "公益诉讼",
)
BEIJING_TIME = ZoneInfo("Asia/Shanghai")
SCORE_LABELS = {
    "haidian_relevance": "海淀相关性",
    "beijing_relevance": "北京相关性",
    "timeliness": "时效性",
    "pain_authenticity": "痛点真实性",
    "policy_window": "政策窗口期",
    "data_verifiability": "数据可验证性",
    "operability": "建议可操作性",
    "mechanism_innovation": "机制创新性",
}
MODEL_SCORE_KEYS = (
    "beijing_relevance",
    "pain_authenticity",
    "policy_window",
    "data_verifiability",
    "operability",
    "mechanism_innovation",
)


def normalize_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", title.lower())


def reported_text(item: EventItem) -> str:
    """规则只读取标题与材料陈述；保留原摘要供后续研究判断。"""
    return item.title + " " + split_discovery_summary(item.summary)['reported_excerpt']


def classify(item: EventItem) -> list[str]:
    text = reported_text(item)
    return [name for name, words in TOPICS.items() if any(word in text for word in words)]


def problem_priority(item: EventItem, config: dict) -> int:
    """只调整排序；问题或征求意见窗口不得因宣传性标题被机械删除。"""
    summary = split_discovery_summary(item.summary)['reported_excerpt']
    if (item.material.get('promotion_suspected')
            and any(word in summary for word in config.get('promotional_material_signals', []))):
        return 1  # 销售性文本不因标题含“陷阱”抢占问题优先位；仍可送模型复核。
    if any(word in item.title for word in ('结构化面试', '模拟题', '备考')):
        return 0  # 问题词出现在练习材料中，不等于真实事件。
    if ('征求意见' in item.title + summary
            or item.source_level == 1 and any(word in item.title for word in ('国标', '国家标准'))
            or any(word in item.title for word in PROBLEM_SIGNALS)):
        return 2
    promotional = any(word in item.title for word in EMPTY_PHRASES + tuple(config.get('promotional_title_signals', [])))
    if promotional and not any(word in summary for word in ('投诉', '纠纷', '退费', '拖欠', '误伤', '转嫁成本', '申诉')):
        return 0
    return 1


def score(item: EventItem) -> int:
    text = reported_text(item)
    value = 25 if item.region == "海淀" else 18 if item.region == "北京" else 7
    value += 18 if item.source_level == 1 else 10 if item.source_level == 2 else 3
    value += min(20, 6 * len(item.topics))
    if any(word in text for word in MECHANISM_SIGNALS):
        value += 15
    if any(word in text for word in PROBLEM_SIGNALS):
        value += 12
    item.timeliness_score = timeliness_score(item.published_at)
    value += item.timeliness_score
    if any(word in item.title for word in EMPTY_PHRASES) and problem_priority(item, {}) == 0:
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


def local_date(published_at: str) -> str:
    if not published_at:
        return ""
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    except ValueError:
        return published_at[:10]
    return published.astimezone(BEIJING_TIME).date().isoformat()


def _distinct_official_document(left: EventItem, right: EventItem) -> bool:
    kinds = {"audit_report", "statistical_release", "enforcement_case", "legislative_oversight_report"}
    if left.source_level != 1 or right.source_level != 1:
        return False
    if any(x.material.get("discovery_provenance", {}).get("material_kind") not in kinds for x in (left, right)):
        return False
    # 差异必须有具体文号、期次或统计期，不能只因URL不同保留同稿转载。
    def identity(item):
        return set(re.findall(r"(?:[（(〔\[]?20\d{2}[）)〕\]]?年?第?\d+号|第[\d一二三四五六七八九十]+[号期]|20\d{2}年(?:\d{1,2}月|第[一二三四1-4]季度)?)", item.title))
    a, b = identity(left), identity(right)
    return bool(a and b and a != b)


def deduplicate(items: list[EventItem], threshold: float) -> list[EventItem]:
    # 先选同URL代表，避免空摘要目录记录抢先占位；不拼接原文或提升信源等级。
    # 仅判断是否有材料陈述，不让假设长度、规则分影响同源取舍。
    by_url: dict[str, EventItem] = {}
    def has_reported_summary(item: EventItem) -> bool:
        excerpt = split_discovery_summary(item.summary)['reported_excerpt'].strip()
        return bool(re.sub(r'^材料陈述\s*[:：]\s*', '', excerpt).strip())

    ordered = sorted(items, key=lambda x: (x.published_at, -x.source_level), reverse=True)
    for item in ordered:
        url = canonical_url(item.url)
        previous = by_url.get(url)
        if previous is None or (has_reported_summary(item) and not has_reported_summary(previous)):
            by_url[url] = item
    kept = []
    for item in sorted(by_url.values(), key=lambda x: (x.published_at, -x.source_level), reverse=True):
        title = normalize_title(item.title)
        if any(not _distinct_official_document(item, old) and
               SequenceMatcher(None, title, normalize_title(old.title)).ratio() >= threshold for old in kept):
            continue
        kept.append(item)
    return kept


def rule_screen(items: list[EventItem], settings: dict) -> list[EventItem]:
    selected, _ = rule_screen_with_decisions(items, settings)
    return selected


def rule_screen_with_decisions(
    items: list[EventItem], settings: dict
) -> tuple[list[EventItem], list[dict]]:
    """使用材料陈述筛选，显式待核假设不参与规则评分与准入。"""
    for item in items:
        item.topics = classify(item)
        item.rule_score = score(item)
    unique = deduplicate(items, settings["title_similarity_threshold"])
    relevant_all = [
        x for x in unique
        if x.topics and x.rule_score >= 35
        and not (any(word in x.title for word in EMPTY_PHRASES) and problem_priority(x, settings) == 0)
        and any(word in reported_text(x) for word in PROBLEM_SIGNALS + EVIDENCE_SIGNALS)
    ]
    ranked = sorted(relevant_all, key=lambda x: (problem_priority(x, settings), x.rule_score, x.published_at), reverse=True)
    selected = ranked[: settings["initial_max"]]
    selected_ids = {id(item) for item in selected}
    unique_ids = {id(item) for item in unique}
    relevant_ids = {id(item) for item in relevant_all}
    exclusions: list[dict] = []
    for item in items:
        item_identity = id(item)
        if item_identity not in unique_ids:
            domain_path = canonical_url(item.url)
            duplicate_url = any(
                domain_path == canonical_url(old.url)
                for old in unique
            )
            reason = "duplicate_url" if duplicate_url else "duplicate_title"
        elif item_identity in relevant_ids and item_identity not in selected_ids:
            reason = "rule_rank_cap"
        elif item_identity in selected_ids:
            continue
        elif not item.topics:
            reason = "no_topic"
        elif any(word in item.title for word in EMPTY_PHRASES):
            reason = "empty_phrase"
        elif item.rule_score < 35:
            reason = "below_rule_score"
        else:
            reason = "no_problem_or_evidence_signal"
        exclusions.append({"event": item, "reason_code": reason})
    return selected, exclusions


def _bounded_score(value, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(maximum, round(value)))


def configured_candidate_score(
    item: EventItem,
    scoring: dict[str, int],
    penalties: dict[str, int],
    *,
    novelty_penalty: int = 0,
) -> tuple[int, dict]:
    """计算正式100分候选分；规则初筛分只保留为溯源信息。"""
    missing = sorted(set(SCORE_LABELS) - set(scoring))
    unexpected = sorted(set(scoring) - set(SCORE_LABELS))
    if missing or unexpected:
        raise ValueError(
            "评分配置键不一致："
            f"缺少={','.join(missing) or '无'}；多余={','.join(unexpected) or '无'}"
        )
    if any(
        isinstance(scoring[key], bool)
        or not isinstance(scoring[key], int)
        or scoring[key] < 0
        for key in SCORE_LABELS
    ):
        raise ValueError("[scoring] 权重必须是非负整数")
    if sum(int(scoring[key]) for key in SCORE_LABELS) != 100:
        raise ValueError("[scoring] 八项正向权重之和必须为100")
    if not penalties or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in penalties.values()
    ):
        raise ValueError("[penalties] 扣分值必须是非负整数")

    analysis = getattr(item, "model_analysis", {})
    model_scores = analysis.get("score_components", {})
    if not isinstance(model_scores, dict):
        model_scores = {}

    raw_timeliness = getattr(item, "timeliness_score", None)
    if not isinstance(raw_timeliness, int):
        raw_timeliness = timeliness_score(item.published_at)
    components = {
        "haidian_relevance": int(scoring["haidian_relevance"]) if item.region == "海淀" else 0,
        # 海淀相关性和北京相关性不重复计分；全国题可以不取得本地分。
        "beijing_relevance": (
            0
            if item.region == "海淀"
            else int(scoring["beijing_relevance"])
            if item.region == "北京"
            else _bounded_score(model_scores.get("beijing_relevance"), int(scoring["beijing_relevance"]))
        ),
        "timeliness": round(
            _bounded_score(raw_timeliness, 20) * int(scoring["timeliness"]) / 20
        ),
    }
    for key in MODEL_SCORE_KEYS[1:]:
        components[key] = _bounded_score(model_scores.get(key), int(scoring[key]))

    applied: list[dict] = []
    seen: set[str] = set()
    raw_penalties = analysis.get("applied_penalties", [])
    if isinstance(raw_penalties, list):
        for entry in raw_penalties:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if key not in penalties or key in seen:
                continue
            # 已由制度新意审查给出扣分时，不再重复扣“政策完整覆盖”。
            if key == "policy_fully_covered" and novelty_penalty:
                continue
            seen.add(key)
            applied.append({
                "key": key,
                "points": int(penalties[key]),
                "reason": str(entry.get("reason") or "模型初筛标记，需人工复核"),
            })
    if item.source_level == 3 and "weak_sources" in penalties and "weak_sources" not in seen:
        seen.add("weak_sources")
        applied.append({
            "key": "weak_sources",
            "points": int(penalties["weak_sources"]),
            "reason": "当前发现来源为三级信源，不能单独支撑核心事实",
        })
    if novelty_penalty:
        applied.append({
            "key": "likely_covered_novelty",
            "points": int(novelty_penalty),
            "reason": "制度新意审查发现现有机制可能已覆盖原缺口，需改写或进一步反证",
        })

    component_reasons = {
        "haidian_relevance": (
            "事件元数据明确指向海淀" if item.region == "海淀" else "事件地域不是海淀，不计分"
        ),
        "beijing_relevance": (
            "海淀相关性已计分，为避免重复不再计北京分"
            if item.region == "海淀"
            else "事件元数据明确指向北京"
            if item.region == "北京"
            else "全国性议题不强制设置北京落点；本项不得分不影响其进入候选"
        ),
        "timeliness": f"发布时间为{local_date(item.published_at) or '未知'}，按时间衰减规则计分",
        "pain_authenticity": str(analysis.get("pain_point") or "模型未提供痛点真实性理由"),
        "policy_window": str(analysis.get("policy_window") or "模型未提供政策窗口理由"),
        "data_verifiability": str(analysis.get("data_assessment") or "模型未提供数据可验证性理由"),
        "operability": str(
            analysis.get("authority") or analysis.get("policy_entry") or "模型未提供可操作性理由"
        ),
        "mechanism_innovation": str(
            analysis.get("policy_entry") or analysis.get("recommendation") or "模型未提供机制创新理由"
        ),
    }
    reasons = {
        "评分版本": "configured_100_v1",
        "正向分项": {
            SCORE_LABELS[key]: {
                "得分": components[key],
                "满分": int(scoring[key]),
                "理由": component_reasons[key],
            }
            for key in SCORE_LABELS
        },
        "正向得分": sum(components.values()),
        "扣分项": applied,
        "扣分合计": sum(item["points"] for item in applied),
    }
    final_score = max(0, min(100, reasons["正向得分"] - reasons["扣分合计"]))
    reasons["最终得分"] = final_score
    return final_score, reasons


def to_candidate(
    item: EventItem,
    index: int,
    scoring: dict[str, int],
    penalties: dict[str, int],
    audit=None,
    novelty_penalty: int = 0,
) -> Candidate:
    analysis = getattr(item, "model_analysis", {})
    topic = primary_topic(item)
    title = analysis.get("suggested_title") or fallback_policy_title(topic, item.title + " " + analysis.get("gap_hypothesis", ""))
    local = (
        "海淀区可直接研究或试点"
        if item.region == "海淀"
        else "北京市可协调或试点"
        if item.region == "北京"
        else "全国性公共利益议题；须在预研中核准有权执行主体、地方试点可能或向上反映路径"
    )
    adjusted_score, configured_reasons = configured_candidate_score(
        item,
        scoring,
        penalties,
        novelty_penalty=novelty_penalty,
    )
    return Candidate(
        id=f"C{index}", title=title, summary=(item.summary or item.title)[:500], event_date=local_date(item.published_at), region=item.region,
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
        eligibility=analysis.get("_eligibility"),
        gap_hypothesis=(audit.gap_hypothesis if audit else analysis.get("gap_hypothesis", analysis.get("policy_gap", ""))),
        gap_type=(audit.gap_type if audit else analysis.get("gap_type", "unclear")),
        coverage_status=(audit.coverage_status if audit else "unchecked"),
        counterevidence=(audit.counterevidence + audit.policy_matches if audit else []),
        novelty_decision=(audit.decision if audit else "proceed"),
        reframe_suggestion=("已有机制可能覆盖原假设；只有发现可验证的执行偏差时才建议改写" if audit and audit.coverage_status == "likely_covered" else ""),
        score_reasons=configured_reasons | {
            "规则初筛分（仅用于模型前筛选）": item.rule_score,
            "事件ID": item.id,
            "来源名称": item.source_name,
            "来源等级": item.source_level,
            "主题": item.topics,
            "来源URL": item.url,
        },
    )


def primary_topic(item: EventItem) -> str:
    text = reported_text(item)
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
