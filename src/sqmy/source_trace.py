"""检查证据材料的可核验性；不以字段完整冒充语义核验。"""


def read_issue(source):
    """旧摘录兼容读取，但不补造全文访问成功记录。"""
    status = source.get("fetch_status")
    if status is not None and not isinstance(status, str):
        return "SOURCE_UNREAD", "fetch_status 未分类或无效"
    if status == "access_failed":
        return "TOOL_FAILURE", "access_failed：来源访问失败，不能据此验证或否定事实"
    if status in {"summary_only", "irrelevant"}:
        return "SOURCE_UNREAD", f"{status}：未取得相关正文证据"
    if status is not None and status not in {"fulltext_ok", "excerpt_verified"}:
        return "SOURCE_UNREAD", "fetch_status 未分类或无效"
    excerpt = source.get("excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return "SOURCE_UNREAD", "缺少必要摘录，URL和来源标签不能代替证据"
    if status is not None:
        locator = source.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            return "SOURCE_UNREAD", "已声明正文核验但缺少定位记录"
    return None


def pre_research_trace(payload):
    sources = {s.get("key"): s for s in payload.get("sources", []) if isinstance(s, dict)}
    checks = []
    for section in ("verified_facts", "evidence_based_inferences", "counterevidence"):
        for index, claim in enumerate(payload.get(section, []), 1):
            if not isinstance(claim, dict):
                continue
            refs = claim.get("source_keys", [])
            if not isinstance(refs, list):
                continue
            for key in refs:
                if not isinstance(key, str) or key not in sources:
                    continue  # 引用完整性由已有验证器处理。
                issue = read_issue(sources[key])
                checks.append({"claim_ref": f"{section}[{index}]", "source_key": key,
                               "code": issue[0] if issue else "TRACE_PRESENT",
                               "reason": issue[1] if issue else "有摘录记录，内容支持强度仍须人工核验",
                               "legacy": sources[key].get("fetch_status") is None})
    reason_map = {
        "policy_covered": "POLICY_DUPLICATE", "insufficient_evidence": "GAP_UNPROVEN",
        "no_local_authority": "AUTHORITY_MISMATCH", "mechanism_not_viable": "NO_MECHANISM",
        "fact_contradicted": "FACT_FALSE", "source_unread": "SOURCE_UNREAD",
        "tool_failure": "TOOL_FAILURE", "claim_too_broad": "CLAIM_TOO_BROAD",
        "low_public_value": "LOW_PUBLIC_VALUE", "high_side_effect_risk": "HIGH_SIDE_EFFECT_RISK",
    }
    codes = {r["code"] for r in checks if r["code"] != "TRACE_PRESENT"}
    if payload.get("decision_reason") in reason_map:
        codes.add(reason_map[payload["decision_reason"]])
    elif payload.get("decision") == "stop":
        codes.add("UNCLASSIFIED_DECISION")
    unknowns = [{"unknown_index": i, "question": u.get("question"),
                 "resolution_plan": u.get("resolution_plan"), "blocking": u.get("blocking")}
                for i, u in enumerate(payload.get("critical_unknowns", [])) if isinstance(u, dict)]
    if any(u["blocking"] for u in unknowns):
        codes.add("GAP_UNPROVEN")
    return {"reason_codes": sorted(codes), "evidence_checks": checks,
            "open_questions": unknowns,
            "note": "原因来自记录和材料状态，不自动判定事实真假；补查不自动解除stop或人工节点"}
