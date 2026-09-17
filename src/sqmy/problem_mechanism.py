"""问题解释的结构与引用检查；不能代替因果或政策内容审查。"""


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_problem(problem, claim_ids, *, deep=False, allow_no_working=False):
    errors = []
    if not isinstance(problem, dict):
        return ["缺少 problem_mechanism 问题机制卡"]
    if problem.get("route") not in {"observed_problem", "policy_interface", "prospective_risk"}:
        errors.append("问题机制 route 未分类")
    for field in ("question", "process_boundary", "process_step", "baseline", "policy_maturation"):
        if not nonempty(problem.get(field)):
            errors.append(f"问题机制缺少 {field}")
    hypotheses = problem.get("hypotheses")
    if not isinstance(hypotheses, list) or len(hypotheses) < 2:
        return errors + ["问题机制须记录主要解释及最强竞争解释"]
    ids = [h.get("id") if isinstance(h, dict) else None for h in hypotheses]
    if any(not nonempty(i) for i in ids) or len(set(i for i in ids if isinstance(i, str))) != len(ids):
        errors.append("问题机制解释 id 必须非空且唯一")
    for h in hypotheses:
        if not isinstance(h, dict):
            errors.append("解释必须为对象")
            continue
        for field in ("statement", "prediction", "discriminating_test", "falsifier"):
            if not nonempty(h.get(field)):
                errors.append(f"解释 {h.get('id')} 缺少 {field}")
        if h.get("status") not in {"supported", "weakened", "unresolved"}:
            errors.append(f"解释 {h.get('id')} 状态无效")
        for field in ("supporting_claim_ids", "counter_claim_ids"):
            refs = h.get(field)
            if not isinstance(refs, list) or any(not isinstance(r, str) or r not in claim_ids for r in refs):
                errors.append(f"解释 {h.get('id')} 的 {field} 存在未知或无效引用")
        if h.get("status") == "supported" and not h.get("supporting_claim_ids"):
            errors.append(f"已支持解释 {h.get('id')} 缺少证据主张")
    chosen = problem.get("working_hypothesis_ids")
    by_id = {h["id"]: h for h in hypotheses if isinstance(h, dict) and nonempty(h.get("id"))}
    if not isinstance(chosen, list) or (not chosen and not allow_no_working) or any(not isinstance(i, str) or i not in by_id for i in chosen):
        errors.append("缺少有效的 working_hypothesis_ids")
    else:
        for key in chosen:
            status = by_id[key].get("status")
            if (status == "weakened" and not allow_no_working) or (deep and status != "supported"):
                errors.append(f"主解释 {key} 尚不足以支持当前阶段结论")
    if deep:
        for field in ("actors", "flows"):
            values = problem.get(field)
            if not isinstance(values, list) or not values or any(not nonempty(v) for v in values):
                errors.append(f"深研问题机制缺少 {field}")
    return errors


def render_problem(problem):
    if not isinstance(problem, dict):
        return []
    lines = ["## 问题机制与竞争解释", "", f"- 研究路径：{problem.get('route', '')}",
             f"- 待解释问题：{problem.get('question', '')}",
             f"- 流程边界与环节：{problem.get('process_boundary', '')}；{problem.get('process_step', '')}",
             f"- 基线：{problem.get('baseline', '')}", f"- 当前政策成熟度：{problem.get('policy_maturation', '')}", ""]
    for h in problem.get("hypotheses", []) if isinstance(problem.get("hypotheses"), list) else []:
        if isinstance(h, dict):
            lines.extend([f"### {h.get('id')}｜{h.get('statement')}（{h.get('status')}）", "",
                          f"- 可观察预测：{h.get('prediction', '')}",
                          f"- 支持主张：{h.get('supporting_claim_ids', [])}",
                          f"- 反证主张：{h.get('counter_claim_ids', [])}",
                          f"- 判别方法：{h.get('discriminating_test', '')}",
                          f"- 证伪条件：{h.get('falsifier', '')}", ""])
    return lines
