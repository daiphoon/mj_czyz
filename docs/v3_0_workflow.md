# v3.0 候选版操作与数据契约

本版是 `3.0.0rc1`。继续使用本地 Python 3.12、SQLite、现有扫描与人工审核流程。研究者整理材料并作内容判断，程序检查结构、引用、版本、预算和状态；通过字段检查不代表事实或政策建议正确。

## 1. 一次正常研究

1. 照常 `preflight --stage scan`、`scan`。新运行在原模型调用内附带研究入口影子意见。候选综合分、名额、排序及 C 编号规则保持不变。
2. Codex 对拟推荐的少数题回源，在已有 `scan_review.md` 说明事实、机制、历史增量、未知与投入。可用 `candidate-review RUN_ID C1 --record FILE` 登记结构化结果，用 `candidates RUN_ID --json` 取得材料哈希及最新记录。登记不自动选题。
3. 用户选择后，按候选新鲜度决定是否先刷新。有限预研使用 `research_contract: research_v3`；旧运行可继续读取旧契约。新版候选不能提交删掉契约标识的旧格式。
4. `pre-research-check` 检查决策单；`stop` 可以完成记录，但不能继续深研。`proceed/reframe` 仍须用户实际确认后登记 `pre-research-review --decision proceed`。
5. 围绕同一问题开展深研，整理逐主张的 `evidence_v2` 证据包。语义复核若有单独试验授权，应在此时用准备好的证据包检查，并计入本题深研额度；默认不调用。
6. `evidence-import FILE`、`evidence-check TOPIC_ID`。登记 `research-brief RUN_ID TOPIC_ID --record FILE`，保存支持与竞争解释、方案比较及成稿/不成稿结论。
7. 写作时读取 `research-brief RUN_ID TOPIC_ID` 返回的最新内容。依据其中的已支持主张及选定方案形成正文，不从发现摘要重新推断事实。研究简报的 JSON/Markdown 阅读副本在 `outputs/review/research_briefs/RUN_ID/`，编辑后须重新登记；任务记录是权威版本。
8. `draft-check`、实际完成四项内容审查、`draft-review`、`draft`、逐页渲染。新审查记录还须绑定简报哈希，并保存正文分项与已选方案的对应。最后仍由用户审核，通过后才执行 `approve`；实际报送后另行登记。

示例位于 `docs/examples/v3/`，全部含占位内容或未审状态。它们用于说明格式，不能当作真实材料或真实通过记录。

## 2. 候选意见与回源推荐

自动初筛 `Candidate.eligibility` 包含 `status`、`reason`、`decisive_unknown`、`verification_entry`，以及程序添加的判断通道、输入冻结时间、材料哈希和影子契约。

| 状态 | 当前含义 |
| --- | --- |
| eligible | 材料提示具体问题、公共价值、权限路径及核验入口，值得有限投入；仍未完成回源 |
| needs_evidence | 关键研究前提待证，保留决定性问题与核验入口 |
| reject | 模型认为当前版本命题不宜继续；影子期不因此删除或重排候选 |
| unavailable | 原调用未给出完整字段；不重试、不补造资格通过 |
| legacy / null | 旧候选没有新版评估 |

人工回源登记使用独立任务 `candidate_review`，包含来源 URL、定位和实际摘录，并绑定 `candidate_sha256`。材料改变后旧登记不会显示为当前记录。它只保存判断者的意见，不代表程序已核实内容，也不替代人工选择与深研许可。

## 3. 预研按停止和继续分别填写

参考 `pre_research.example.json` 与 `stop.example.json`。

停止记录保留决策分类、停止依据、来源获取状态、关键未知/反证、放弃条件、预算理由、实际投入和重开条件。尚未取得材料时，事实、推断、反证和方案可为空；不能为了字段完整而制造已读来源。`record_valid=true` 只说明停止记录完整，`research_allowed` 仍为 false。

继续题沿用两条原始信息链及至少一个一级来源等既有门槛，保留权限、反证、阻断未知和人工确认。新增最小问题机制卡：具体研究问题、流程边界和环节、现行安排及政策成熟度；主要与最强竞争解释，各自的可观察预测、支持/反证主张、判别方法、证伪条件和当前状态。预研先用 `initial_feasibility` 检查权限、重大风险与低成本替代，完整方案压力测试留到深研。

`observed_problem`、`policy_interface`、`prospective_risk` 分别对应具体问题、政策接口和前瞻风险。选择这些标签不自动证明损害、因果或权限成立。`decision_reason` 为标准分类；`causal_gap` 与资料不足、政策覆盖、工具失败分开。旧停止记录不自动补造新分类或重开。

## 4. 逐主张来源片段

证据包新增 `evidence_contract: evidence_v2`。每个 `claims[].sources[]` 关系保留原 `source/role/origin_group/source_level/primary_source`，并添加 `evidence_detail`：

| 字段 | 含义 |
| --- | --- |
| claim_part | 当前来源支持或反驳主张中的哪部分 |
| locator | 表格、段落、页码或其他可复查位置 |
| excerpt | 必要片段；不批量收集全文 |
| excerpt_kind | verbatim 原文、paraphrase 转述、unavailable 未取得 |
| support_scope / limitation | 能证明什么、不能推出什么 |
| fetch_status / checked_at | 该片段实际获取状态和核验时间 |
| context | 语义复核需要的必要原文条件、例外、表注、时间或对象限定 |
| excerpt_sha256 | 程序计算；如提供现有哈希，须与摘录一致 |

同一来源可对不同主张保存不同摘录。来源的另一个片段读过，不能替本片段兜底。作者转述不能仅靠 `excerpt_verified` 就变成直接原文。`access_failed`、`access_restricted`、`source_explicitly_not_public`、`source_unread` 等均不能冒充已读证据。

旧 `evidence_v1` 仍可读取，原指纹按旧字段计算；升级至 v2 后不能删除标识降级。导入仍是现有主张的幂等新增/更新，不通过从文件删行暗中删除历史主张。问题解释和方案使用独立 `research-brief`，不能作为证据包中被忽略的字段。

## 5. 深研简报与写作

`research_brief.example.json` 说明完整结构。简报须绑定当前运行、候选、题目、最新 `pre_research_review_id` 和 `evidence_sha256`。

成稿结论 `write` 要求 evidence_v2 的证据闸门通过、无阻断未知、主解释有对应证据；各方案通过 `target_mechanism_ids` 指向已支持解释。至少比较 `baseline` 与 `minimum`；必要时再加入 `stronger`。新措施复用原完整机制卡和三情形压力测试，补权限、权利影响、实施能力、结果和预警指标。`selected_option_ids` 可以选择基线，不强制新增干预。

若研究反证了原解释，可以保存 `no_draft`、剩余未知和重开条件，不强制设计干预方案。程序据此阻止成稿。每个新内容版本保留独立任务；原文件丢失时可从相同内容重建，不能重新提交旧版本恢复旧通过记录。

内容审查增加 `research_brief_sha256` 和 `problem_option_map`。后者将正文问题编号映射到简报已选方案，检查范围不止编号数量。仍需实际完成事实、机制反方、问题—建议对应、文风结构四项内容审查。

## 6. 语义影子复核与成本

默认 `[semantic_review].enabled=false`，模式只支持 `shadow`。以下命令只准备输入，零模型调用：

```bash
sqmy semantic-review RUN_ID TOPIC_ID --package data/sources/TOPIC_evidence.json
# 读取已经导入的 evidence_v2；或读取当前证据对应的已有意见：
sqmy semantic-review RUN_ID TOPIC_ID
sqmy semantic-review RUN_ID TOPIC_ID --show
```

只有在明确的小样本试验范围和原深研行为剩余额度下，启用配置并显式添加 `--run` 才会调用 provider；`--retry` 只重试同一失败行为，不清零账本。非 live 运行禁止真实调用。模型、DeepSeek 备用与思考设置仍来自现有模型配置，不为该工具擅自更换。

每次最多 3 条核心主张、16,000 字符必要输入，输出估算上限 2,048 Token；这些均来自配置，未增加深研总上限。材料超界时拒绝，不自动截掉条件或例外，也不自动拆批扩题。直接原文或必要上下文不齐时记 `review_unavailable`，不判断事实错误。

输入去掉作者推荐、置信度、解释结论及自述支持范围，保留原材料和主张本身。输出逐来源支持关系及组合关系，取值为 `fully_supports/partially_supports/does_not_support/contradicts/cannot_determine`；地域、时间、人群、数字口径、语气、条件、因果、政策效果、同源问题使用独立问题代码。独立 provider 上下文不等于人工盲审或模型能力已验证。

真实请求先在 `model_calls` 占额，固定复用 `RUN_ID + deep_research + TOPIC_ID`。失败及未知用量照计，备用单独计次。证据导入、简报登记作为深研耐久边界，交互估算仅补足原阶段上限扣除已记模型用量后的余量；已有估算不向下释放。若之前已经占满 40,000 深研阶段 Token，则不能再追加语义调用，需按原预算调整制度另行处理。读缓存和准备输入不收费。

`--adjudication FILE` 登记回到原文后的内容裁决，包含当前复核哈希、审查者、时间和各主张 retain/narrow/supplement/delete 的依据。它不直接改写证据。收窄、补证或删除必须另行修订材料，再重新检查；四项内容审查与用户审核仍然保留。

`human_effort` 可选记录 `review_minutes`、`major_fact_changes`、`major_mechanism_changes`，未知用 null 或省略。不要估造人工时间或把程序运行耗时当人工工作量。

## 7. 冻结回放与恢复

继续使用 `scripts/replay_workflow_revision.py`。`--freeze-spec SPEC --bundle-dir NEW_DIR` 冻结显式文件；`--check-bundle DIR` 检查哈希；`--evaluate-bundle DIR` 使用包内配置和截止日期运行预研确定性检查。

spec 每例包含 `case_id/as_of/files`。每份文件说明相对路径、role、`available_at` 和 `availability_basis`。必要角色为 materials、policy_context、history_context、settings、model_context；材料晚于截止时间不混入，缺文件、可知依据或历史上下文均标为 `incomplete_replay`。预研评估要求一份材料 JSON 和一份配置 JSON/TOML；不完整案例不借用当前配置执行。它不运行模型，也不利用后来结论补旧题。

`scan-replay` 仍是按当前确定性规则复用旧输出的诊断；与完整冻结信息条件的实验区分。当前 10 例历史基准包均缺部分当时依赖，所以不能据此开展严格新旧模型效果比较。

回退时可将候选影子模式设为 off，保持语义真实调用关闭；保留新字段读取器和已经登记的版本。旧运行仍按旧契约恢复，不删除新列、研究简报或审批记录，不直接用旧数据库覆盖升级后的工作。数据库恢复须先列明备份后新增材料及处理方案。
