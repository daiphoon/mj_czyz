"""只供离线测试的文本与审查记录，不代表真实内容审查或报送材料。"""
import json
from datetime import datetime, timezone

from sqmy.delivery import REVIEW_KINDS, check_draft, register_review


def valid_text():
    background = "本文件是送审程序的离线测试样例，不陈述真实政策事实。测试只验证文本长度、结构、版本绑定和文件完整性，不能据此判断实际制度效果，也不得作为报送材料。"
    problem = "测试场景中的信息由处理方掌握，申请方无法直接核验处理过程。评估时应区分规则存在与实际可用，核对材料流转路径及异议处理责任。不能把未检索到公开文件写成制度不存在，也不能用个案推导整体情况。"
    proposal = "建议在测试环境中保留最小必要凭证，使受影响对象可以核对处理结果。验证应包含正常情形、复杂对象和异常退出，不以材料数量代替结果质量。核验失败时允许重新说明和纠正，不将成本转嫁给申请人；无法证明收益时停止扩大实施范围。"
    return ("# 关于完善海淀区测试机制的建议\n\n中关村支部：李智\n\n## 一、现状\n\n" + background * 3
            + "\n\n## 二、问题和分析\n\n" + "\n\n".join(f"（{n}）这是问题。" + problem * 2 for n in "一二三")
            + "\n\n## 三、政策建议\n\n" + "\n\n".join(f"（{n}）这是建议。" + proposal * 2 for n in "一二三") + "\n")


def review_record(settings, run, topic, source):
    check = check_draft(settings, topic, source)
    payload = {key: check[key] for key in ("topic_id", "source_sha256", "evidence_sha256", "problem_ids")}
    payload.update(claim_ids=check["critical_claim_ids"], reviewed_at=datetime.now(timezone.utc).isoformat(),
                   reviews={kind: {"status": "passed", "reason": "离线夹具验证记录字段，不代表真实内容审查", "reviewer": "fixture"} for kind in REVIEW_KINDS})
    record = source.with_suffix(".review.json")
    record.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return record


def record_valid_review(settings, run, topic, source):
    return register_review(settings, run, topic, source, review_record(settings, run, topic, source))
