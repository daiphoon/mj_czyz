# 社情民意信息研究与生成工作流

这是一个面向海淀区民建区委报送场景的本地、可恢复、可追溯工作流。当前版本已接入真实公开来源的低成本发现层，并保留 mock 回归流程。所有自动候选和 mock 稿均带有“不得直接报送”约束。

当前为 **v3.0 发布候选版 `3.0.0rc1`**。新研究接通逐主张摘录、分阶段预研、竞争解释、方案比较和版本化写作输入；候选资格保持影子观察，旧评分与排序继续使用，真实语义调用默认关闭。参见 [v3.0 操作说明](docs/v3_0_workflow.md)、[实施验收记录](docs/v3_0_implementation.md)及[变更说明](CHANGELOG.md)。研究质量收益与正式行为切换尚待真实样本验证。

## 在Codex对话中启动

日常只需说：“运行社情民意正常扫描，由当前对话完成初筛。”无须提供模型标识、复制JSON或手动分三步。Codex自动先预检、用 `scan-prepare` 准备一次有界冻结材料，由当前主对话读取并判断，再通过本地文件和 `scan-import` 严格校验、保存候选。主对话自身仍消耗订阅额度；程序没有为这一步启动另一个模型调用。界面模型标识与官方Token无法可靠取得时，如实记为 `unknown` 与估算，不改模型或思考深度。

下面的命令由Codex处理，供维护者核对：

```bash
sqmy preflight --stage scan-prepare
sqmy scan-prepare
# 当前主对话读取 data/runs/RUN_ID/conversation_screening.json
# 依据 materials.prompt 与 materials.schema 作判断，自动写出 result_envelope
sqmy scan-import RUN_ID --result /path/to/conversation_result.json
# 中断收尾：从SQLite已接受结果恢复，无需原结果文件，不重新采集或判断
sqmy scan-import RUN_ID --resume
```

冻结包绑定运行、输入、Schema、配置、实现版本和有效期，准备及导入都检查研究 `stop/no_draft`。未知或重复ID、缺字段、额外字段、错误类型、越界分数和重复JSON字段均整份拒绝，允许零题或少于5题。相同结果幂等，不同结果不覆盖，扫描和导入共用独占锁。过期或材料变化须报告并等待新的有界任务，不自动扩扫。候选评分、同源合并及人工选择仍沿用现有规则；待收尾的中间候选不能被选择。

导入本身不联网，不调用Provider或Tavily，只按本地政策机制库复核；报告明确标记未做新增网络反证，不能据此声称政策覆盖核验完成。Codex仍须按项目规则对少数拟推荐题回源并查反证，之后由用户选题及放行研究。桥接到实际主对话的自动读取、传递和判断尚未进行真实运行验证，离线测试不能证明这部分体验或质量。

对话筛选记为 `execution_mode=current_conversation`、`model=unknown`、`official_usage=null`。程序用冻结提示、Schema和结果长度形成 `artifact_proxy_estimate`，幂等记入 `stage_usage`，不伪造 `model_calls` 或官方Token。它不覆盖完整上下文、推理和重做，可能低估；实际订阅消耗仍需由用户能取得的用量信息补充观察。

统一检索和回源接口的边界、费用开关及恢复方式见 [检索流水线一期](docs/retrieval_pipeline.md)。免费模式不读取Tavily账户密钥；Brave和旧Tavily计费入口默认关闭。当前 `retrieve` 保留查询搜索，自动页面正文获取暂隔离，明确记录未读和 `RESEARCH_INCOMPLETE`；这不影响固定来源元数据采集，也不等于完整Fetch已修复。

## 独立CLI兼容路径

使用 Python 3.12：

```bash
uv sync --extra dev --python 3.12
source .venv/bin/activate
sqmy init
sqmy --model MODEL_ID preflight --stage scan
sqmy --model MODEL_ID scan
sqmy --model MODEL_ID scan --start-tier 2
sqmy --model MODEL_ID scan --clues data/inbox/daily_clues.jsonl
sqmy --model MODEL_ID scan --resume RUN_ID
sqmy scan-replay RUN_ID
# 从输出复制 RUN_ID
RUN_ID=2026-01-01-xxxxxxxx
sqmy candidates "$RUN_ID"
sqmy candidate-pool
sqmy select "$RUN_ID" C1
# 候选超过24小时或事件快速变化时执行零模型复核：
sqmy preflight --stage refresh
sqmy refresh "$RUN_ID"
sqmy refresh "$RUN_ID" --decision keep --note "人工核对后未发现重大变化"
sqmy --model MODEL_ID preflight --stage pre_research --run-id "$RUN_ID"
sqmy --model MODEL_ID pre-research-check "$RUN_ID" C1 --brief data/sources/TOPIC_pre_research.json
# 查看预研决策单；确认后才放行深研：
sqmy pre-research-review "$RUN_ID" C1 --decision proceed --note "确认按改写后的问题清单深研"
sqmy --model MODEL_ID preflight --stage research --run-id "$RUN_ID"
sqmy --model MODEL_ID evidence-import data/sources/TOPIC_evidence.json
sqmy evidence-check TOPIC_ID
# 新版研究还须登记完整研究简报，绑定最新预研批准和证据哈希：
sqmy --model MODEL_ID research-brief "$RUN_ID" TOPIC_ID --record data/sources/TOPIC_research_brief.json
# 仅对正式研究实际使用、且内容易变化的核心公开来源执行：
sqmy evidence-snapshot data/sources/TOPIC_evidence.json SOURCE_KEY \
  --reason dynamic_content --file /path/to/page.pdf
sqmy --model MODEL_ID preflight --stage writing --run-id "$RUN_ID"
sqmy draft-check TOPIC_ID --source outputs/review/deep_research/RUN_ID/formal_draft.md
sqmy draft-review "$RUN_ID" TOPIC_ID --source outputs/review/deep_research/RUN_ID/formal_draft.md --record data/sources/TOPIC_draft_review.json
sqmy --model MODEL_ID draft "$RUN_ID" TOPIC_ID --source outputs/review/deep_research/RUN_ID/formal_draft.md --candidate-id C1
sqmy approve TOPIC_ID
# 仅在人工实际完成报送后登记：
sqmy mark-submitted TOPIC_ID --date 2026-01-08 --level "海淀区"
sqmy pause "$RUN_ID" --quota
sqmy resume "$RUN_ID"
sqmy generate-mock "$RUN_ID"
sqmy status "$RUN_ID"
sqmy budget
sqmy budget-adjust --stage screening --old-limit 55000 --new-limit 65000 \
  --reason "本次人工批准将具体新线索从8条增至12条，仅用于一次受控初筛" \
  --expected-benefit "比较新增线索并尽早证伪已有政策覆盖的假设"
sqmy budget-review ADJUSTMENT_ID --actual-tokens 61200 \
  --actual-benefit "新增候选并排除伪缺口" --decision reassess
sqmy --model MODEL_ID provider-check
# 旧 --simulate-codex-quota 参数已停用，不会调用备用服务。
sqmy novelty-report --days 21
sqmy discovery-report
sqmy skip-run RUN_ID --reason "候选质量不足"
sqmy cleanup
sqmy cleanup --apply
# 如后续人工复核发现早期阻断正确、误杀或应改写（预研结果会自动反馈，无需重复执行）：
sqmy novelty-review RUN_ID:EVENT_ID confirmed_block --reason "一级来源已明确覆盖原缺口"
```

发现阶段在入池及提供商入口前复用真实研究的 `stop`、人工停止和 `no_draft` 记录。旧待筛队列、换标题和同源转载不能自动重开；停止记录保持可读且不批量改写历史。取得关键新证据或原判断纠错依据后，须在新的预研版本填写绑定材料的 `reopen_assessment`，通过原有闸门并取得新的人工确认。操作、字段和恢复限制见 [停止研究与有证据的重开](docs/research_stops.md)。

也可在未安装项目时运行：

```bash
PYTHONPATH=src python3 -m sqmy.cli init
```

## 运行规则

### Tavily 可选补充通道

保留原有RSS和四层来源扫描。Tavily不是主搜索替代品，也不保证能读取所有被限制的网页；搜索结果和摘录仍是待核材料。

- `scan` 在RSS采集后，按当前较少出现的具体痛点选择最多4条场景检索词；同等条件按日期轮换，不重复提交全部RSS关键词。Tavily使用 `news`，每条最多5项、摘要最多700字，不请求AI答案、图片或网页全文，仍进入原有去重和最多12条入模池。
- 人工选题后使用固定问题单：查新线索用 `discovery/news`（90天），查现行政策/原始依据用 `policy/general`（不设新闻时间限制）。原有RSS制度新意快审和影子核验保持原规则；不自动把所有反证查询都改成付费调用。
- 保存发布时间、事件时间、更新时间及待核状态。缺日期不补造，也不一律丢弃；仅有更新时间不代表新事件。材料用途、疑似推广和同源转载为提示，不新增硬性准入闸门；国际页面须核对与中国的具体关系。
- Tavily来源按发布域名归并，避免把检索词数量当来源多样性；不同域名仍不等于独立原始信息链。已确认政策覆盖仍须依据原始文件及其适用范围。
- `retrieve pages` 暂时不执行HTTP、DNS或Extract，也不通过已有缓存或旧collector取得正文。每行为仍最多登记2页，记录 `unsupported_transport/body_read=false/source_unread`；搜索摘要和来源属性不能代替已读原文。底层Fetch安全检查保留，完整传输兼容性另行验收。

当前默认Tavily keyless搜索不读取或发送账户密钥，免费入口失败也不隐式切换为密钥计费。旧 `.env` 与计费字段仅保留兼容，不因此次回退启用付费入口；不得将密钥写入问题单、日志或输出。mock、fixture、回放或refresh不会自动调用补充API。

参数在 `[tavily]`。初始每行为10积分、等价费用0.08美元上限，搜索最多4次、提取最多2次；金额和次数分别在请求前拦截，不保证用满所有名额。默认高级搜索每次按2积分预留；单页高级提取按2积分保守预留，并保留不足5页时的分摊成本。积分不是模型Token，美元是配置单价估值，不是实际账户账单；原模型单行为Token及DeepSeek金额预算不变。价格和接口语义依据 [Tavily计费说明](https://docs.tavily.com/documentation/api-credits)、[Search](https://docs.tavily.com/documentation/api-reference/endpoint/search) 和 [Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)，2026-09-06核验。

在本项目目录的终端中操作（问题单由Codex按人工选题边界编制）：

```bash
sqmy --model MODEL_ID preflight --stage scan
sqmy --model MODEL_ID scan
# 预研示例先复制docs/retrieval_plan.example.json，改为已选候选和具体问题。
sqmy retrieve --run-id RUN_ID --plan data/sources/TOPIC_retrieval_plan.json
sqmy retrieval-usage RUN_ID
# 中断后先使用完全相同的运行和问题单；成功步骤不会重做。
sqmy retrieve --run-id RUN_ID --plan data/sources/TOPIC_retrieval_plan.json
# 新检索策略下显式重试失败查询，仍共用原额度；页面不重试，旧检查点仅复用。
sqmy retrieve --run-id RUN_ID --plan data/sources/TOPIC_retrieval_plan.json --retry-failed
```

初轮问题单包含 `stage`、`candidate_id`、`queries`、`pages`；显式补查可增加绑定最新决策单的 `repair`，字段见[补查说明](docs/research_workflow_revision.md)。每条query须说明用途，每页须有目标词；新路由不要求预先填写HTTP失败原因，页面登记仍保持未读。`pre_research`要求已人工选题；`research`还要求最新预研闸门通过和人工proceed。问题单、检索参数及页面隔离策略一经执行即固定；恢复不能改词扩题，补查也不增加原行为额度。新检索使用v2策略，原固定采集v1契约保持不变；旧检索检查点只读复用，不重新派发或转到旧付费入口。`--diagnostic`含queries时会真实搜索，不得反复新建诊断规避同一行为预算。

检查点和检索报告位于 `data/runs/RUN_ID/`，SQLite `retrieval_calls` 是积分权威账本，`retrieval_calls.json`为可重建审计副本。成功输入永久用于同一行为恢复；跨运行相同请求按24小时复用精简结果。硬中断和失败未知用量保留占额，不自动重试、不自动切换DeepSeek。可选发现通道失败时停止Tavily补充、记录原因，已有RSS继续原流程；预研检索失败则保存步骤并要求明确恢复。旧 `.env` 不因程序运行被覆盖。

接下来沿用来源漏斗和预研反馈，观察“独有有效线索、预研通过/有效停止、成稿贡献、每次有用决策的积分和Token”；当前只完成工程验收，不能声称产量已经提高。小诊断默认仅JSON/Markdown和调用审计，不再默认生成HTML仪表盘、图表或Notebook。

### 常规运行与质量边界

2026-09-07的[候选研究入口方法](docs/candidate_entry_trial.md)已接入原有入口，无需另贴提示词或换命令。新 `sqmy --model MODEL_ID scan`（含 `monday` 兼容入口）的初筛输入将显式标注的上游设想与材料陈述分开，二者都不自动成为已核事实；实际初筛提示要求中性命题、材料依据及决定性未知，保留事件、政策衔接和前瞻性风险路径。原摘要、输出Schema、排序规则、证据闸门与预算不变。旧运行恢复沿用原提示版本和缓存。

在本项目中让Codex按正常方式启动扫描后，Codex按 `AGENTS.md` 对少数拟推荐题复用证据并在已有授权和剩余额度内回源，将依据、政策边界、研究价值、未知及投入写入原扫描复核文件；独立CLI仍只输出初筛报告，不会自动读完原文或批准研究。选题后用同一事实材料作一次有边界的重构，事实前提不足仍停止。模型是否更少继承推断、候选质量和后续成稿收益尚待真实运行观察。

此前仅改Tavily公共服务场景的一条检索词的试行继续观察，本次不扩大查询、来源或预算。随后3次用户发起的正常扫描分别记录该组是否暴露及新输入方法的使用情况，不以离线接线验证代替成稿收益。

- 任意日期扫描：用户需要时运行一次 `sqmy --model MODEL_ID scan`。项目通常每个自然日至多扫描一次，检索近90天公开信息，缓存RSS元数据，完成时间过滤、URL还原、去重、主题分类和规则筛选，生成最多5个新候选；每周1—2篇仍是质量目标，不设每日成稿指标。
- 模型前去重：真实运行会先对比之前真实运行的URL、“标题+发布日”及同日同机构同主题的跨站转述，内容未变化或属于同源转载时不再进入模型。测试、mock、诊断和回放运行不会污染真实历史。
- 四层完整扫描：每次扫描覆盖“海淀和北京主题源→北京增补权威源→全国部委、监管、司法、统计和调查源→投诉、论坛和社交平台待核线索”。前层凑够8条后也不提前停止；所有来源先用零模型规则处理，最终单次模型输入仍限制12条。
- 跨日发现队列：全部规则合格事件写入SQLite轻量队列。入模池按高分排序，并分别给外部补充线索、新鲜事件和久候事件保留配置名额，避免“等待时间长”压过高质量新线索。调用模型前还要通过批次规模、来源多样性、高分事件及“足够新鲜或足够强”的零模型质量闸门；不足时继续入队并提示补充来源。相同URL内容未变化不会重复进入模型，标题或摘要发生实质变化时重新开放。
- 来源与阶段漏斗：每次扫描按来源记录原始RSS结果、时间窗内结果、有效元数据、规则合格、历史排除、模型输入、模型入选和最终候选数。21天报告还把候选回连到预研通过、成稿、人工通过和报送；只有达到真实运行数和观察跨度门槛后，连续3次零有效结果或长期无规则合格结果的来源才标记为 `degraded`，且只建议检查抓取或检索词，不会自动停用来源。详细记录位于 `data/runs/RUN_ID/discovery_observability.json`，SQLite 表为 `source_funnel`。
- 候选前影子核验：对模型前池做元数据级的事件角色、北京落点、原始一级来源和已知政策覆盖检查；必要时只做受限且可缓存的搜索元数据查询。该步骤新增模型调用为0，结果位于 `data/runs/RUN_ID/candidate_shadow_review.json`。滚动评测只把后续预研明确标为 `policy_covered` 或 `original_gap_supported` 的结果用于准确率，不把普通 `reframe` 强算为政策覆盖；当前不影响模型输入、候选排序或阻断。
- 分层续扫：可使用 `sqmy --model MODEL_ID scan --start-tier 2|3|4` 从指定层另建可恢复运行，不重新处理更早层级。常规完整扫描直接使用 `sqmy --model MODEL_ID scan`。
- 终层无候选：已完成全部可用扩展层且没有候选时，运行自动标记为 `skipped`，保存明确停止原因并将下一步设为 `none`；不得以旧题、换标题或未经高等级来源核验的三级线索补足数量。只有出现新事实、新数据、新政策偏差或不同机制切口时，才可另行复核近90天暂缓候选或历史题。
- 新事件补位：规则筛选后先排除历史重复和跨站同源事件，再从剩余新事件中取前12条，避免旧事件占满模型前名额。
- 地域判定：来源检索通道地域与事件实际地域分别保存；事件地域必须有可信地方域名、标题/材料陈述或人工导入时单独填写的明确地域依据，“北京电”等发稿地不算北京事件。新行为重核待筛记录时保留非空的 `clue_import:` 依据，但这不等于完成事实核验，摘要假设及来源通道标签仍不能单独支撑地域。
- 筛选缓存：完整初筛输入哈希相同时，先复用已完成的结构化结果，再决定新调用；观察期不以Token阻断，显式恢复额度策略后缓存仍可复用。
- 时效性：发布7天内20分、30天内15分、60天内8分、90天内3分；时间不明不得分。
- 候选100分评分：规则分只用于模型前低成本筛选。最终候选分实际使用 `config/settings.toml` 的 `[scoring]` 八项权重和 `[penalties]` 扣分；海淀与北京相关性不重复计分。全国题在北京相关性项可为0，但不再仅因缺少北京落点自动扣分；仍须核准有权主体、公共价值和可执行路径。
- 离线回放：`sqmy scan-replay RUN_ID` 使用已保存的模型结果重新运行标题、历史、新意和报告步骤，新增模型调用为0。
- 强制重算：只有人工明确要求时使用 `sqmy --model MODEL_ID scan --force`；它会跳过历史排除和模型结果缓存。`--screen-now` 只提前处理队列，不绕过去重、缓存、预算或模型前质量闸门；命中紧急条件的事件仍可小批量处理。
- 分行为预检：`sqmy --model MODEL_ID preflight --stage scan|refresh|pre_research|research|writing [--run-id RUN_ID]` 检查SQLite、配置、目录、残留任务、人工闸门和对应的单行为额度，模型调用为0。有限预研、深研和写作必须传入 `--run-id`，以确认人工选题和行为边界。筛选额度不足不会阻止零模型元数据入队；`refresh` 不要求模型额度、Codex CLI或新增调用成本。
- 全部放弃：候选质量不足时使用 `sqmy skip-run RUN_ID --reason "..."`，不让运行长期停在 `needs_review`。
- 安全清理：`sqmy cleanup` 只预览并逐项给出删除理由；`--apply` 会先使用SQLite在线备份，再清理测试、mock、空运行和临时输出，最后执行完整性检查。DOCX中间渲染默认保留7天，每个QA组中编号最高的最终渲染、逐页图片、PDF和版式摘要长期保留。RSS缓存、真实运行、最新回放、提供商诊断、证据包和研究报告默认保留。
- 离线回归：`sqmy scan-mock` 生成5个纯 mock 候选；`sqmy --model MODEL_ID scan --fixture PATH` 使用离线RSS夹具。
- 人工选择：`sqmy select RUN_ID C1 [C2]`，最多 2 个。
- 新鲜度复核：候选在扫描后24小时内可直接进入有限预研；超过24小时、事件快速变化或临近正式写作时，运行 `sqmy refresh RUN_ID` 做零模型增量发现，再通过同一命令的 `--decision` 和 `--note` 记录保留、修订或替换决定。正式稿导出会阻断过期或尚未人工确认的复核结果。
- 近期候选池：`sqmy candidate-pool` 列出最近30天仍未选择且未关闭的最多5个真实候选。旧候选不会重新调用模型，实际选择时仍按24小时规则复核。
- 有限预研：`pre-research-check` 导入结构化决策单。新版 `research_v3` 按停止/继续分配材料负担：停止题可不写完整方案，继续题比较主要与竞争解释，并保留事实、反证、关键未知、权限、预算和人工门槛。完整方案压力测试放在深研。新契约须填写标准化 `decision_reason`，旧记录保持兼容。有效预研回写制度新意反馈，不覆盖人工标签。`proceed` 或 `reframe` 仍须人工 `pre-research-review --decision proceed`，阻断项不能人工越过。格式与示例见 [v3.0 操作说明](docs/v3_0_workflow.md)。
  - 停止反馈区分记录完整性（`record_valid`）与深研许可（`research_allowed`）：字段及基本校验合格、但有阻断性未知的 `stop` 也会写入停止反馈；字段错误不计为有效反馈。纳入反馈不改变深研闸门，旧决策单可用原路径重复执行 `pre-research-check` 补记，不重复累计阶段Token或制造新研究结论。
- 正式起草：深研仍由人工与Codex协作完成。固定结构 Markdown 必须先取得预研人工放行，再经 `evidence-check` 放行，才可使用 `sqmy draft` 确定性导出送审 DOCX；它不会调用模型。改稿覆盖同名送审文件时，旧导出任务会标记为 `skipped`，缓存只有在文件 SHA-256 与任务记录一致时才复用，避免旧输入哈希误指向新版文件。
  - 2026-09-07证据传递修复：预研报告显示摘录与限制，获取失败不能当作已核证据；辅助事实缺证同样挡在写作前。审批只绑定候选最新决策版本。`retrieve`支持显式绑定关键未知的有限补查，全部轮次仍共用原调用和积分上限，详见[研究证据传递与有界补查](docs/research_workflow_revision.md)。
- 审核和报送：`sqmy approve` 仅记录人工通过并复制到 `outputs/submission/`，不会发送材料；只有人工实际报送后才能执行 `mark-submitted`。
- mock 隔离：`generate-mock` 只用于测试，真实流程没有名为 `generate` 或 `export` 的模糊命令。
- 暂停/恢复：`sqmy pause RUN_ID --quota` 保存原阶段和下一动作，`sqmy resume RUN_ID` 只恢复暂停或失败运行。发现阶段通过 `sqmy --model MODEL_ID scan --resume RUN_ID` 使用同一运行ID继续。

扫描恢复加固：新运行在模型调用前原子保存 `data/runs/RUN_ID/scan_input.json`，固定本次输入池；恢复不重新采集或吸收新线索。模型结果和用量在同一数据库事务落库，后续报告失败时复用结果；完整制度审查另存检查点，队列只在候选持久化后标为已处理。同一工作区同时只允许一个扫描进程。强制退出留下的 `running` 运行可直接执行 `sqmy --model MODEL_ID scan --resume RUN_ID`，程序取得独占锁后才允许接管。旧运行没有输入快照时仍走原有采集/缓存路径，不能承诺恢复其已丢失的原始输入池。
- 失败继续：`sqmy retry RUN_ID`。
- 提供商诊断：`sqmy --model MODEL_ID provider-check` 仅在明确批准后验证Codex主通道；旧 `--simulate-codex-quota` 参数立即返回停用错误，不调用服务或建立运行。
- 正式 DOCX 模板：`templates/submission_template.docx`。
- DOCX 字体与渲染：导出器对样式和文本 run 均显式写入等线；macOS 下使用 Word 私有等线字体进行 LibreOffice QA 的方法见 `docs/template_artifact.md`。
- mock 稿：`outputs/review/RUN_ID/`，不会自动进入 `outputs/submission/`，也不会自动发送。
- 证据审查：`evidence-import` 导入轻量“主张—来源”包，`evidence-check` 核对独立来源链、直接反证和现有政策覆盖。主张还需标注 `verified_fact`、`evidence_based_inference`、`unverified_hypothesis` 或 `analyst_judgment`，以及置信度、截至日期、不确定性和必要的可证伪条件；核心统计必须保存时间、地域、总体、单位和定义。未分类主张、假设、分析判断、过期核心来源和未解决冲突不能进入正文。没有主张、没有核心主张或存在阻断核心主张时均采用 fail-closed。
- 动态证据快照：`evidence-snapshot` 必须点名证据包中的单一来源、说明固化理由，并要求该来源已填写正文用途 `used_at`。可复制已有PDF/HTML，也可抓取公开URL；保存抓取时间、关键摘录、最终URL、内容哈希和大小，不发送Cookie或登录凭证，不批量固化稳定法规页面。快照位于本地 `data/sources/snapshots/TOPIC_ID/`，不自动提交到Git。
- 制度新意快审：模型初筛时同步产生一句可被反证的缺口假设、缺口类型和最多2条定向检索词。程序先查本地政策机制库，再做标题和摘要级反证检索。区级 `site:` 检索会保留本区查询并追加市级及中央官方范围查询；反证政策使用独立的730天窗口，不受普通候选90天窗口限制。只有“制度不存在”且被已核验机制直接覆盖时才自动阻断；执行、效果、协同和问责类缺口只标记复核并扣分，不自动删除。

## 安全和成本

### 第三批质量加固后的送审步骤

在本项目根目录运行，且先完成有限预研放行和证据导入：

```bash
sqmy draft-check TOPIC_ID --source 稿件.md
sqmy draft-review RUN_ID TOPIC_ID --source 稿件.md --record 审查记录.json
sqmy --model MODEL_ID draft RUN_ID TOPIC_ID --source 稿件.md
```

`draft-check` 是零模型检查，返回正文有效字数、结构错误、核心主张编号和两个版本指纹。正文按汉字、字母、数字计数，不含标题、署名、空白、标点；普通稿通常目标1200—1500字，偏离目标给出提示；硬范围仍为1200—1800字，必要扩展须在审查理由中说明。重大稿用 `--major` 检查2000—2500，并在记录中说明扩展理由。规则位于 `[writing_quality]`，旧配置缺少目标参数时沿用原硬范围。正式正文禁止URL、邮件及Markdown链接；导出、缓存复用和审核还检查DOCX超链接关系及HYPERLINK字段，内部证据底稿可保留来源链接。问题和建议须各有3—5项、编号对应。确定性检查不能判断事实真伪、因果是否成立或政策是否真正激励相容。

“高度重视”等措辞只生成上下文审查提示，不仅凭词语出现就自动退稿（例如正文可能在反对空泛表述）。文风审查须判断其是否缺少具体机制；不能用消除关键词替代内容审查。

按 `docs/draft_review.example.json` 建立四项内容审查记录，完成真实审查后再填入 `passed`、审查者和有依据的理由；示例默认 `needs_review`，不能直接放行。`draft-review` 只登记结果，不调用模型，也不代替内容审查。稿件或证据改变后，旧记录不再适用，必须重新检查和审查。导出与人工通过之间还核验DOCX哈希，防止审核对象被替换；新流程待报送文件变动后不能直接登记报送。已审核或已报送的旧稿不追溯改状态，但无新版本审查记录的历史稿不被包装成已通过新标准。

发现层修正了两类误判：查询失败只记为失败，不当作政策覆盖；异地或尚未生效的机制只保留为比较线索，不用于阻断当前题。关键词匹配仍不是完整语义证明，正式研究继续执行人工和证据闸门。不同文章的 `id` 等查询参数保留，只有跟踪参数被去除，避免同路径多篇文章被误合并。

来源目录新增国家统计局数据发布、国家发展改革委通知两个 `html_index` 直连入口，每次各最多15条，仅读取目录标题、日期和URL；保留原40组搜索来源，不增加模型输入池或Token上限。目录无可解析日期时记录解析异常，不抓正文补数、不自动停源。2026-09-06已验证页面可读；这只证明采集可用，不证明能提高可成稿率，仍需滚动真实运行评估。

模型前历史复核：`[history_review]` 控制已审核真实稿的群体/标题与问题/建议机制重叠提示，每题最多2条短提示，随同原初筛调用处理，不新增模型调用。提示不自动删除、扣分或改变排序；新群体、新事实或新机制可以保留，入选说明须交代差异。候选报告同样显示提示，不能把字符重叠率当作语义重复概率。

待筛排序先看具体问题和政策窗口，再看原规则分；低问题密度会议、成绩介绍后排，含具体问题或国标实施窗口的材料不因宣传性词语直接删除。`premodel_current_clue_reserve=2` 从原3个外部名额中优先留给本次导入且规则合格的线索，旧第4层积压不能占满这些名额；总输入上限12、来源及预算闸门保持不变。保留名额不等于信源质量豁免。

疑似推广标记同时命中 `promotional_material_signals` 的销售性文本，降低问题优先级而不直接删除；报道中只出现“广告”等字样不降级。近日期长摘要高度重合、没有已识别数字冲突的转述在选池及模型输入中合并，优先较高等级来源，原采集记录仍保留。恢复只整理冻结的输入，不从队列补题；`screening_materials.json` 留下原输入、实际输入及合并关系。这种相似度合并仍需内容复核，不代表独立信源验证。

`model.codex_timeout_seconds` 保留420秒超时；重试2次、同一行为最多3次模型请求仍是可靠性边界。`screening_item_chars=0` 取消为节省Token设置的解释字数目标，要求充分说明依据、未知与研究价值。新的发现摘要视图保留最多700字并分隔材料陈述与假设，仍只处理轻量元数据；旧冻结契约继续复用。`screening_max_output_tokens`、`diagnostic_max_output_tokens` 仅用于观察估算，不能视为Codex CLI服务端硬上限。语义复核的输入容量、材料获取范围以及正式正文的1200–1500字目标/既有硬范围仍保留；这些是上下文可靠性、隐私或文稿用途约束。超时和无效结果仍作为失败保存，不以半截结果成候选。

产量报告保留发现周转化归因，新增 `calendar_draft_count`、`calendar_approved_count`、`calendar_submitted_count` 按实际完成日期计数。稳定产量只看连续已结束自然周的 `calendar_draft_count`：必须有绑定版本审查的首次合格导出事件和实存文件，不能只靠 `evidence_gate=passed` 或topics行数。修订同一课题不重复计篇，跨周完成的旧题计入完成周；旧稿缺少可核验记录时单列 `unverified_legacy_draft_count`，不猜测补记。

密钥只放 `.env`，不得提交。2026-10-01 用户决定停用DeepSeek；默认 `provider = "codex_cli"`，仅使用本机Codex通道。额度或速率限制进入 `paused_quota`，不自动切换。旧 `provider=auto` 只构建Codex主通道；`provider=deepseek`、旧备用诊断及直接DeepSeek客户端均返回 `provider_disabled`，不读取DeepSeek凭据或发请求。账户和原有密钥不删除。

初筛在空临时目录中执行，提示要求只处理所给元数据；`--sandbox read-only` 不是禁用全部工具，不能仅凭提示声明证明文件隔离。真实诊断仍须确认订阅认证、实际接收方、工具权限及发送范围。调用记录保存提供商、模型、提示哈希、用量和估算成本；本地Codex成本记为0不能证明认证方式、剩余额度或无额外credits账单。Tavily能力保留，实际请求须按需并确认使用已有额度、无新增账单。

本项目不设置固定模型或思考深度。Codex对话中的日常初筛优先采用上述准备/导入入口，由用户当前主对话作判断，无须模型ID。独立CLI `scan`、语义复核 `--run` 和诊断仍兼容 `sqmy --model MODEL_ID COMMAND`，缺少参数时在读取凭据、初始化运行或采集前停止；不能可靠读取界面选择时不得猜测或使用旧默认值。二者必须明确区分，不能把对话路径说成UI选择已自动传入独立 `codex exec`。

参数只对本次进程生效，不写项目模型默认值或全局Codex设置。CLI调用显式传 `--model`，不强制 `max/medium` 等思考深度；旧 `screening_model/codex_model` 配置和旧冻结字段均不能供新调用回退。新初筛材料和调用账本记录传入值；已完成缓存保留原记录，不把复用描述为新模型调用。新运行的冻结模型与恢复参数不同且没有完成缓存时停止，不能静默切换，须先明确处理该有界行为及剩余预算。预研、深研和写作由当前Codex对话进行，程序仅登记其产物和估算；登记时可同样传入 `--model`，未提供时记录 `unknown`，不冒充自动识别的会话模型。DeepSeek模型、思考和单价字段只作已停用通道的历史兼容。

观察期默认 `budget.enforce_token_limits=false`，全流程阶段额度、调用前Token预留、累计Token及历史用量均不阻断已授权行为，不必逐次提额。原初筛55000、预研30000、深研40000、写作15000和诊断30000数值仅保留为比较参考与显式恢复政策的配置。`sqmy budget` 输出 `token_limit_policy=observe_only`，分列提供商实报量、失败/未知请求的保守估算和交互产物估算；估算不是官方Token或账单。当前预研可声明 `budget.token_estimate`（兼容旧 `token_limit`）；深研证据/简报及写作按耐久产物代理估算，阶段内幂等取最大，已有历史估算不向下清零。新观察不再用固定额度冒充已耗用量，完整对话消耗仍可能低估。

调用前先在SQLite事务中登记尝试、Token和费用预留，再发送请求；返回可靠用量后替换预留，返回内容解析失败也不丢弃用量。`max_calls_per_action` 按实际提供商尝试计数，失败、超时和恢复重试均累计；同一输入还受 `max_retries` 限制，不自动循环重试。历史备用尝试、费用预留和失败用量仍保留，不清账或追补无法确认的历史用量；当前没有DeepSeek直连或自动备用请求。

调用记录以SQLite为准，`data/runs/RUN_ID/model_calls.jsonl` 是原子更新、可重建的审计副本，不含提示全文、密钥或错误响应正文。`provider_reported` 表示返回用量；`conservative_estimate` 表示无可靠用量的估算；`reserved_estimate` 表示尚未核算的预留。程序被强制结束时，无法确定服务端是否已经完成或收费；恢复保留预留，并允许在剩余行为额度内重试，因此不能保证这种情况下绝不重复收费。未结算在途金额不随7日窗口自动释放，需要取得可核验用量后再人工处理；不得通过删除账本来腾出额度。旧版漏记的失败调用无法从现有账本追溯补造。

预算的主要作用是防止单一行为扩题、过度调研或反复重试。调用前估算已超上限时，系统在模型前安全暂停；单次在途调用无法可靠中断，若实际用量超限，系统先保存当前行为的有效结果和超额原因，但不自动扩题或进入下一模型阶段。再次执行或扩大范围前，应先复盘并调整对应行为额度。真实订阅或 API 额度耗尽仍安全暂停。

2026-10-01 用户批准暂停Token预算阻断，优先观察质量与真实消耗。`budget-adjust`、`budget-review` 及原配置保留供历史比较或将来显式恢复；仅改数值不会重新开启硬闸门，也不应要求用户为日常已授权工作逐次提额。调用次数、超时、重试、人工选题/研究/送审、来源与证据边界、每日扫描节奏继续保留，不自动无限扩题。DeepSeek仍停用；付费API未获授权，Tavily仍只在既有确认无新增账单的有界额度内按需使用。

来源配置位于 `config/sources.toml`。搜索RSS只承担发现功能，记录中保存的是还原后的原始页面URL；候选阶段不批量下载网页全文。

RSS 请求必须返回有效的 `rss/channel` 结构才会写入缓存；HTML 首页、错误页、空响应或损坏 XML 均记为失败。尚未过期的旧异常缓存保留并报错，不自动重抓；有效空 RSS 才表示查询成功但没有结果。反证查询的解析错误向上层传递为失败，不得解释为未发现政策覆盖。2026-09-20 本机验证发现 Bing 新闻入口仍重定向至首页，尚未恢复四层有效覆盖；普通搜索 RSS 的日期和新闻适用性未通过核验，未作为替代入口。详见 [RSS 修复记录](docs/rss_repair_20260920.md)。

经批准的官方目录试验新增海淀医保公开案例、北京市市场监管动态、北京市根治欠薪通知公告三个直连来源，每个目录最多 15 条，只提取标题、原始链接和目录显示日期，不自动读取文章或附件正文。目录日期不等于事件发生时间；转载仍须回源核准。海淀医保目录只识别已核验的空 `strLink` 静态链接分支，不执行脚本；页面结构改变或跳转不明时按解析限制处理。原有四层来源及 RSS 仍保留，新增来源不代表四层覆盖已恢复。试验结果见 [官方目录评估](docs/official_directory_trial_20260920.md)。

2026-09-20 发现体系重构一期再接入北京市人大报告、审计公告、政民互动公开答复及最高法权威发布，共9个直连目录。公开答复记录目录日期依据，不把来信陈述当作政府核实事实。`discovery.rss_endpoint_circuit_breaker=true` 时先探测一个Bing新闻RSS查询；明确返回HTML则将同轮其他查询标为 `skipped_endpoint_unavailable`，不删除来源，不把超时或有效空RSS当作共同故障。新一轮重新检查入口。

`data/runs/RUN_ID/discovery_coverage.json` 按URL去重分列渠道、发布域名、材料类型、地域及信源等级；`discovery_observability.json` 同时保存规则合格和实际模型输入的覆盖维度。这些标签不验证原始信息链，不改变评分。查询角色规划可通过Tavily可选 `discovery_roles` 启用，但当前短查询样本未证明收益，配置仍保留原查询和advanced深度。

投诉、论坛和社交平台不能依靠新闻RSS稳定覆盖。需要补充时，由 Codex 使用当前网页检索能力为本次扫描找到最多12条公开线索，按 `docs/clue_input.example.jsonl` 格式保存，再运行 `sqmy --model MODEL_ID scan --clues PATH`。程序只保存标题、公开URL、发布日期和不超过700字的摘要，拒绝含Token、Cookie或会话参数的URL，并将规范化输入快照保存到本次运行目录以便恢复。

线索文件只放具备具体群体、具体场景和可反证制度缺口的候选形态事件。仅列出多个宽泛类别的热线月报、投诉总量或行业汇总不单独占用模型名额；它们应并入具体线索摘要作为规模或交叉验证材料，待有限预研再核对口径。

常规规则池为三级线索保留2个最低核验名额；跨日队列另为第4层外部补充线索保留3个名额，并同时受单来源上限和模型前质量闸门约束。这些都是防止有价值线索被挤出的最低留位，不是必须填满的配额，也不是可信度豁免。三级线索必须在有限预研中找到独立高等级来源；否则停止，不得进入正式稿。

发现层可观测参数位于 `[observability]`，影子核验参数位于 `[shadow_verification]`。每次完成扫描后会自动更新 `outputs/review/discovery_observability_rolling.json`，也可用 `sqmy discovery-report` 零模型重算；该报告只统计真实 `live` 运行，同时达到配置的最少运行数和至少14天观察跨度后才标记为可比较，同一天重跑不能冒充2—3周验证。来源健康和制度覆盖影子结论都只用于人工复盘，不自动修改来源、规则、预算或阻断状态。

每个来源通过 `expansion_tier` 标记层级：1为海淀、北京主题源，2为北京新增权威源，3为全国权威与调查源，4为投诉、论坛和社交平台待核线索（现有实现也承载全部Tavily结果，不能据此判断信源等级）。全国性议题不强制拥有北京落点，但须说明有权执行主体、地方试点可能或向上反映路径。

证据规则位于 `config/settings.toml` 的 `[evidence]`。多家媒体转述同一发布会、报告或数据源时只计为一个原始信息链；一个正式一级原始来源可以单独支撑其直接公布的事实。企业自报默认只证明“已公开宣称某机制”，不证明实际效果。

证据安全：三级来源及 `pain_signal` 只作线索，不计入核心事实交叉验证链。统计口径五项必须填写非空文本；无效分类、缺失或未来核验时间会阻断，导入时间不等于核验时间。程序核对的是记录一致性，不代表自动核实了网页内容。

每个课题可使用自己的主张编号（例如 `c1`）；内部编号自动隔离。`source_usages` 保存本题的摘录、正文位置、来源用途和核验日期，同一网页用于其他课题不会覆盖本题记录。旧数据库采用增量迁移，保留旧主张编号、来源和关联；旧来源曾跨题共享时标记待复核。出现该提示，应核对本题原始证据包后重新 `sqmy evidence-import PATH`，再执行 `sqmy evidence-check TOPIC_ID`，不要直接清除标记。无法从旧数据库恢复已经被覆盖的原始内容。

清理安全：`sqmy cleanup` 仅预览；未知名称的人工目录和经过符号链接的路径不进入自动删除计划。仍只删除明确属于可丢弃测试运行的目录及已定义的可再生产物，不执行真实研究目录清理。

已核验的制度机制位于 `config/policy_mechanisms.toml`，每条都必须保存来源和最后核验时间。制度库自动匹配除满足关键词数量外，还必须命中至少一个主题特异词，只有“平台、投诉、举报”等通用词重合时不得判为已有政策覆盖。默认超过180天未重新核验的机制不再用于自动阻断，只作为复核线索。反证搜索会自动限定政府、法院和网信等一级来源；模型给出区级 `site:` 检索词时，同时保留本区查询并追加市级及中央官方范围查询。反证政策默认回看730天，避免把仍可能有效但早于普通90天新闻窗口的上位政策漏掉；命中仍只作为覆盖警告，是否现行有效须在预研中核验。程序会排除候选新闻自身和同标题页面。每次扫描完成后会自动重新生成 `outputs/review/metrics/novelty_rolling_21d.json`。报告一方面滚动统计审计量、阻断量、自动预研反馈、早期漏判、新增及重新开放事件、待处理队列、审计Token、模型前排除量、扩展层级、缓存命中和节省Token；另一方面按发现运行所在自然周归集“候选—选择—预研—深研—证据闸门—成稿—通过—报送”漏斗、停止原因及项目已记录Token。稳定性只使用已经结束的自然周：连续4个完整自然周每周至少1篇通过证据闸门的送审稿，才标记 `stable_minimum_output=true`；2篇目标单独统计。当前周单列，不会因某天没有新候选而提前判定失败。其中“潜在浪费防止量”是代理指标，交互式订阅Token若无法计量也会明确说明，不得当作实际账单节省。

## 测试

```bash
uv run --python 3.12 --with python-docx --with pytest python -m pytest -q
```

详细设计见 `docs/architecture.md`，模板证据见 `docs/template_artifact.md`。

### 摘要筛选与目录日期修正（2026-09-20）

经用户批准，规则主题、分数、准入、优先级、地域提示、查询补缺与紧急触发只使用标题和显式分隔后的材料陈述，待核假设仍保留供研究判断。未分隔旧摘要按原文兼容，仍需回源；并不表示程序能识别所有未标注推断。新扫描会在内存中重核含显式假设的旧待筛材料，不改写原队列或已冻结运行。

同一URL既有空摘要目录记录、又有必要段落摘要时，先保留有材料陈述的完整记录再筛选，同源仍只算一次；显式假设不作为优先保留依据。两者均有陈述或均为空时沿用日期、信源等级顺序，不拼接摘要、不提升信源等级，原始记录及排除原因留在审计中。修复只作用于新筛选，恢复仍使用原冻结输入。

北京市公开答复来源的日期依据改为 `listing_displayed_date`；来信时间与答复时间可能不同，回源记录分别保存，未读正文时不补造。此次不修改主题词、相似标题阈值、模型池大小和预算。


### 小范围质量改进操作（2026-10-01）

失败诊断在同一事务以 `tasks.kind=provider_failure` 关联调用ID，并随 `data/runs/RUN_ID/model_calls.jsonl` 导出 `diagnostic`。它只含退出码、HTTP状态（适用时）、安全类别和固定有限摘要，不保存原始stderr、命令、提示或凭据；无法确认的错误为 `unknown`。旧失败没有诊断时保持原记录，不反推根因。阅读运行报告时引用对应调用ID及诊断；未知Token继续保守估算，普通失败不自动切换付费备用。读取/重建审计副本不要求重试真实请求。

下一次原本计划的正常扫描，可在既有授权和额度内选不超过 `project.max_formal_topics` 条调查报道或公开办理材料，复用已读内容，仅补必要段落摘要，按现有线索导入规则分开“材料陈述”和“待核假设”，保留处理结果、反证、发生时间与地域依据。不是新增必做步骤，不批量抓全文、不另开扫描或付费额度。沿用 `scan_review.md` 记录新增独立问题、入口复核、后续预研停止原因和是否成稿；规则通过或候选变多不等于质量改善。一次只观察摘要补充这一因素，不同时改来源、评分和模型名额。


## 下一次质量与消耗观察

实现验收结束后，由用户另外确定一次正常有界扫描，不自动连续运行。沿用 `RUN_ID_scan_review.md` 与现有账本，最多复核主选、备选各1题：记录实际读到的原始来源、具体事实和限定、现行政策为何仍未解决、增量与权限、决定性未知及研究可达性。0题也是有效结果；区分来源不足、事实前提不成立、政策覆盖、历史重复和工具失败。

用户选题并放行后才进入后续阶段；以证据通过且完成四项内容审查的实际送审稿（通常1200–1500字，署名中关村支部：李智，正文无超链接）衡量可报送成果，再记录重大修改和人工复核时间。未知留空，不用运行耗时冒充人工耗时，候选命中数或字段通过不等于质量改善。

消耗分列程序实报量、失败未知量、主对话声明/产物代理估算、可取得的订阅用量及Tavily积分/费用。主对话重做、上下文和思考不能由产物长度精确还原，缺少实际用量时明确限制；将来判断保留、回退或继续观察，须同时看事实依据、政策增量、返工与实际成果，不能只看模型调用次数。
