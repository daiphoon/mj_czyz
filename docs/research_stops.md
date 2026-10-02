# 停止研究与有证据的重开

2026-10-01 起，真实发现使用预研 `stop`、人工 `stop` 和深研 `no_draft` 的登记记录约束模型池。历史文件仅作为阅读副本，账本记录为准；fixture、mock 和回放的研究记录不进入真实停止判断。不批量改变旧候选、审批或发现队列。`--force` 也不能越过研究停止结论。

每轮 `data/runs/RUN_ID/research_stop_gate.json` 保存匹配理由、原停止依据、原重开条件、来源入口和 `event_content_sha256`。签名由规范化 URL、标题和“材料陈述”组成，规则分或假设文字改变不能单独解除停止。未满足条件的旧待筛事件保留监测，不进入模型；报告显示暂缓数量，不沿用旧批准。

确有关键新证据时，先准备新的 `research_v3` 有限预研版本，通过既有 `pre-research-check RUN_ID CANDIDATE_ID --brief PATH` 与 `pre-research-review RUN_ID CANDIDATE_ID --decision proceed --note NOTE` 登记。重开不是自动深研许可转移，也不新增额度。允许利用同一原来源纠正原判断，但必须明确纠错依据；新标题、更多同源报道、一般政策或只写长一点的摘要不构成新证据。

在新决策单中增加如下字段（替换全部示例占位值）：

```json
{
  "reopen_assessment": {
    "stop_ids": ["从本轮停止审计复制相关停止记录ID"],
    "event_content_sha256": "从本轮停止审计复制目标材料的64位SHA256",
    "basis": "new_evidence",
    "condition_met": "具体说明新记录如何回答原停止条件，区分主要解释和竞争解释",
    "new_evidence_claim_ids": ["F_NEW"]
  }
}
```

`F_NEW` 必须位于该版本 `verified_facts`，引用一级或二级来源，记录已读必要正文的 `excerpt_verified/fulltext_ok`、摘录、定位及限制。新证据模式要求引用材料相对于原停止版本确有不同；同一 URL 的新必要段落可使用。纠正原判断时将 `basis` 改为 `correction` 并增加具体 `correction_reason`。相关多项停止条件均须回应。

只有新版本闸门允许研究、最新版本已获新的人工 `proceed`，且目标材料签名和停止记录均对应时，发现层才允许重新比较。程序检查引用、读取状态、版本及批准，不能判断摘录是否在语义上真正解决关键未知；人工须回到必要原文裁决。仅给 URL、检索摘要、旧批准、无对应主张或未批准新版本均不能重开。

如果停止状态或重开依据在冻结输入后变化，原提示与缓存不能继续使用。重新有界准备当前输入并复核预算，保留原运行的冻结材料和调用历史；不自动重试或另外申请调用。
