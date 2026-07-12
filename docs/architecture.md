# 第一版技术设计

## 边界

第一版只建立可运行骨架和 mock 闭环，不接入批量网站抓取或真实 OpenAI 调用。所有外部调用未来均通过可记录、可预算、可恢复的任务接口进入。

## 模块

- `config`：TOML 配置和路径解析。
- `db`：SQLite 历史库、运行库、来源库、模型调用审计库。
- `workflow`：两阶段状态机、幂等候选生成、人工闸门、暂停和恢复。
- `budget`：任务预留和全周 Token 上限。
- `document`：从保留的参考 DOCX 创建显式样式模板并导出正式结构。
- `cli`：本地命令入口。

## 数据模型

`runs` 保存运行级检查点；`tasks` 保存幂等输入哈希和步骤结果；`candidates` 保存候选及分项理由；`topics` 保存历史选题；`sources` 保存轻量溯源；`claims` 保存正文可能使用的事实、政策和制度新意主张；`claim_sources` 保存来源的支持/反证角色与原始信息链；`model_calls` 保存模型调用、Token 和估算成本。

## 证据质量闸门

深研不再只统计网页数量。每个关键主张都记录 `origin_group`：转述同一发布会、原报告或数据库的页面只算一个来源链。关键主张可由一个正式一级原始来源支撑，或由至少两个独立来源链交叉验证。

制度新意主张还必须完成现行法规、政策、司法建议和主管机制覆盖审查。状态为 `single_source`、`conflicted`、`needs_review` 或 `rejected` 的关键主张会阻断正式写作。系统允许深研结论为“不成稿”。

## 候选阶段制度新意快审

模型初筛不再直接产出最终5题，而是最多产出8个备选并为每题记录 `gap_hypothesis`、`gap_type` 和最多2条 `counter_queries`。`novelty` 模块先查已核验的本地政策机制库，再在真实运行中完成最小化的标题、摘要反证检索。

自动阻断采用不对称规则：仅 `policy_absence` 类主张被本地已核验机制直接覆盖时标记为 `covered / block_original_gap`。执行、效果、协同和问责类主张即使命中既有机制，也只标记为 `likely_covered / keep_with_novelty_warning`，不自动删除。检索无结果只能得到 `unclear`，不能证明制度不存在。

`novelty_audits` 保存每次快审、决策、反证、复核结果和代理Token指标。每次周一运行都会重新计算滚动21天指标。自动结论依次区分 `insufficient_sample`、`no_block_cases_yet`、`needs_more_review_labels`、`review_false_blocks_before_tightening` 和 `continue_current_gate`。阻断准确率只能在有人工或后续研究标签时计算，不使用系统自己的阻断结果循环证明自己正确。

## 模型调用前的去重与复用

`run_context` 区分 `live`、`test_fixture`、`mock`、`diagnostic` 和 `replay`，只有 `live` 运行进入历史排除和21天质量指标。规则筛选后的模型输入池会先核对历史URL、“标题+发布日”，以及“同日、同发布机构、同核心主题”的跨站转述，未变化或同源事件不进入模型。完整输入哈希相同时，`tasks` 跨运行复用已完成结果。

周一扫描依次使用三层候选池：第一层扩展现有北京、海淀来源可识别的主题内容；不足8条新事件时，第二层加入北京法院、检察、市场监管和权威媒体等来源；仍不足时，第三层加入全国来源。历史和同源排除发生在截取模型前12条之前，保证排名稍后的新事件可以补位。三层只运行规则，不重复调用模型；达到8条即停止扩展，或在第三层穷尽后对现有1—3条执行一次初筛。`run_efficiency` 记录扩展层级、模型前数量、历史或同源排除量、实际模型输入量、缓存命中、缓存节省Token和候选数，滚动21天报告自动汇总，供未来2—3周评估。`monday-replay` 可以在不新增模型调用的情况下重新验证后续规则。`--force` 仅供人工明确要求全量重算时使用。

## 扫描前维护

`preflight` 是零模型调用的只读预检，覆盖SQLite完整性、残留任务、TOML解析、必需文件、写入目录、Codex CLI、DeepSeek备用、`.env` 权限和周/阶段预算。任一阻断项失败时返回非零状态。

`cleanup` 默认只产生清理计划。`--apply` 先调用SQLite backup API写入 `data/history/backups/`，再按外键顺序删除无价值运行数据和临时路径，最后 `VACUUM` 并运行 `integrity_check`。保留范围为所有真实运行、最新回放、提供商诊断、源数据、缓存、模板和耐久研究记录。

## 状态机

`discovery → selection(needs_review) → incremental_review → research → writing → export(needs_review)`。

任一步骤可进入 `failed`、`paused_quota`、`paused_budget` 或 `skipped`。每次转换立即提交 SQLite 事务；大文件产物先写临时文件再替换（真实采集阶段实现）。任务以 `run_id + kind + input_hash` 保证输入未变化时复用结果。

## Token 控制

预算全部位于 `config/settings.toml`。真实模型接入时先预留任务预算，调用后写入实际 Token；超过任务、阶段或周预算即保存检查点并进入 `paused_budget`。额度错误进入 `paused_quota`。候选阶段只传标题、摘要和结构化元数据；深研只处理人工确认的 1—2 题；写作只接收结构化研究摘要和已核验证据。

## 真实周一发现层

`collector` 从 `config/sources.toml` 的受控检索通道收集近90天标题、摘要、时间和原始URL。搜索RSS仅作为发现入口，Bing跳转链接会还原为来源页面；响应按查询哈希缓存6小时，不批量下载正文。

来源通道地域与事件实际地域分开保存。事件地域只依据可信地方域名或标题、摘要中的明确地域证据推断；“北京电”等媒体发稿地不会被当成事件发生地。地域判断依据保存在 `region_evidence`，便于复核。

`screener` 完成主题分类、活动宣传硬过滤、URL与相似标题去重和规则评分。采集结果先压缩为15—30条，再取8—12条进入模型层。默认 `provider=auto`：主通道以只读、临时会话运行本机 `codex exec`，使用ChatGPT/Codex订阅；只有明确的额度或速率错误才切换DeepSeek JSON API。普通失败不切换，避免掩盖程序或质量问题。每次结果均写入 `model_calls` 和运行级结构化审计文件。

候选报告只输出最多5题；不足时允许少于5题，不以低质量事件补数。候选保留原始URL、来源等级、历史标题相似度和“不得直接报送”标记。
