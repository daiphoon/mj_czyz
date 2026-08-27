# 社情民意信息研究与生成工作流

这是一个面向海淀区民建区委报送场景的本地、可恢复、可追溯工作流。当前版本已接入真实公开来源的低成本发现层，并保留 mock 回归流程。所有自动候选和 mock 稿均带有“不得直接报送”约束。

## 快速开始

使用 Python 3.12：

```bash
uv sync --extra dev --python 3.12
source .venv/bin/activate
sqmy init
sqmy preflight --stage scan
sqmy scan
sqmy scan --start-tier 2
sqmy scan --clues data/inbox/daily_clues.jsonl
sqmy scan --resume RUN_ID
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
sqmy pre-research-check "$RUN_ID" C1 --brief data/sources/TOPIC_pre_research.json
# 查看预研决策单；确认后才放行深研：
sqmy pre-research-review "$RUN_ID" C1 --decision proceed --note "确认按改写后的问题清单深研"
sqmy evidence-import data/sources/TOPIC_evidence.json
sqmy evidence-check TOPIC_ID
sqmy draft "$RUN_ID" TOPIC_ID --source outputs/review/deep_research/RUN_ID/formal_draft.md --candidate-id C1
sqmy approve TOPIC_ID
# 仅在人工实际完成报送后登记：
sqmy mark-submitted TOPIC_ID --date 2026-01-08 --level "海淀区"
sqmy pause "$RUN_ID" --quota
sqmy resume "$RUN_ID"
sqmy generate-mock "$RUN_ID"
sqmy status "$RUN_ID"
sqmy budget
sqmy budget-adjust --stage screening --old-limit 26000 --new-limit 45000 \
  --reason "扩大候选池前进行一次受控初筛" \
  --expected-benefit "发现新增候选并尽早证伪已有政策覆盖的假设"
sqmy budget-review ADJUSTMENT_ID --actual-tokens 70912 \
  --actual-benefit "新增候选并排除伪缺口" --decision reassess
sqmy provider-check
sqmy provider-check --simulate-codex-quota
sqmy novelty-report --days 21
sqmy skip-run RUN_ID --reason "候选质量不足"
sqmy cleanup
sqmy cleanup --apply
# 如后续人工复核发现早期阻断正确、误杀或应改写（预研结果会自动反馈，无需重复执行）：
sqmy novelty-review RUN_ID:EVENT_ID confirmed_block --reason "一级来源已明确覆盖原缺口"
```

也可在未安装项目时运行：

```bash
PYTHONPATH=src python3 -m sqmy.cli init
```

## 运行规则

- 任意日期扫描：用户需要时运行一次 `sqmy scan`。项目通常每个自然日至多扫描一次，检索近90天公开信息，缓存RSS元数据，完成时间过滤、URL还原、去重、主题分类和规则筛选，生成最多5个新候选；每周1—2篇仍是质量目标，不设每日成稿指标。
- 模型前去重：真实运行会先对比之前真实运行的URL、“标题+发布日”及同日同机构同主题的跨站转述，内容未变化或属于同源转载时不再进入模型。测试、mock、诊断和回放运行不会污染真实历史。
- 四层完整扫描：每次扫描覆盖“海淀和北京主题源→北京增补权威源→全国部委、监管、司法、统计和调查源→投诉、论坛和社交平台待核线索”。前层凑够8条后也不提前停止；所有来源先用零模型规则处理，最终单次模型输入仍限制12条。
- 跨日发现队列：全部规则合格事件写入SQLite轻量队列。普通新事件不足4条时先积累，不再因来源层已经穷尽而强制调用模型；达到4条、最早事件等待48小时、命中紧急条件或人工使用 `--screen-now` 时才初筛。相同URL内容未变化不会重复进入模型，标题或摘要发生实质变化时重新开放。
- 来源与阶段漏斗：每次扫描按来源记录原始RSS结果、时间窗内结果、有效元数据、规则合格、历史排除、模型输入、模型入选和最终候选数。详细记录位于 `data/runs/RUN_ID/discovery_observability.json`，SQLite 表为 `source_funnel`。程序每个排除阶段只保存配置数量的标题和URL样本，用于复查误杀，不保存全部搜索结果或网页全文。
- 候选前影子核验：对模型前池做元数据级的事件角色、北京落点、原始一级来源和已知政策覆盖检查；必要时只做受限且可缓存的搜索元数据查询。该步骤新增模型调用为0，结果位于 `data/runs/RUN_ID/candidate_shadow_review.json`；当前仅供21天滚动比较，不影响模型输入、候选排序或阻断。
- 分层续扫：可使用 `sqmy scan --start-tier 2|3|4` 从指定层另建可恢复运行，不重新处理更早层级。常规完整扫描直接使用 `sqmy scan`。
- 终层无候选：已完成全部可用扩展层且没有候选时，运行自动标记为 `skipped`，保存明确停止原因并将下一步设为 `none`；不得以旧题、换标题或未经高等级来源核验的三级线索补足数量。只有出现新事实、新数据、新政策偏差或不同机制切口时，才可另行复核近90天暂缓候选或历史题。
- 新事件补位：规则筛选后先排除历史重复和跨站同源事件，再从剩余新事件中取前12条，避免旧事件占满模型前名额。
- 地域判定：来源检索通道地域与事件实际地域分别保存；事件地域必须有可信地方域名或标题、摘要中的明确证据，“北京电”等发稿地不算北京事件。
- 筛选缓存：完整初筛输入哈希相同时，先复用已完成的结构化结果，再检查新调用预算；缓存命中不会因剩余预算不足而被拦截。
- 时效性：发布7天内20分、30天内15分、60天内8分、90天内3分；时间不明不得分。
- 候选100分评分：规则分只用于模型前低成本筛选。最终候选分实际使用 `config/settings.toml` 的 `[scoring]` 八项权重和 `[penalties]` 扣分；海淀与北京相关性不重复计分。全国题在北京相关性项可为0，但不再仅因缺少北京落点自动扣分；仍须核准有权主体、公共价值和可执行路径。
- 离线回放：`sqmy scan-replay RUN_ID` 使用已保存的模型结果重新运行标题、历史、新意和报告步骤，新增模型调用为0。
- 强制重算：只有人工明确要求时使用 `sqmy scan --force`；它会跳过历史排除和模型结果缓存。`--screen-now` 只提前处理小批量，不绕过去重、缓存或预算。
- 分阶段预检：`sqmy preflight --stage scan|refresh|research|writing` 检查SQLite、配置、目录、残留任务和相应阶段预算，模型调用为0。筛选预算不足不会阻止零模型元数据入队，但模型步骤会安全暂停；`refresh` 不要求模型额度、Codex CLI或新增调用成本。
- 全部放弃：候选质量不足时使用 `sqmy skip-run RUN_ID --reason "..."`，不让运行长期停在 `needs_review`。
- 安全清理：`sqmy cleanup` 只预览；`--apply` 会先使用SQLite在线备份，再清理测试、mock、空运行和临时输出，最后执行完整性检查。RSS缓存、真实运行、最新回放、提供商诊断、证据包和研究报告默认保留。
- 离线回归：`sqmy scan-mock` 生成5个纯 mock 候选；`sqmy scan --fixture PATH` 使用离线RSS夹具。
- 人工选择：`sqmy select RUN_ID C1 [C2]`，最多 2 个。
- 新鲜度复核：候选在扫描后24小时内可直接进入有限预研；超过24小时、事件快速变化或临近正式写作时，运行 `sqmy refresh RUN_ID` 做零模型增量发现，再通过同一命令的 `--decision` 和 `--note` 记录保留、修订或替换决定。正式稿导出会阻断过期或尚未人工确认的复核结果。
- 近期候选池：`sqmy candidate-pool` 列出最近30天仍未选择且未关闭的最多5个真实候选。旧候选不会重新调用模型，实际选择时仍按24小时规则复核。
- 有限预研：`pre-research-check` 导入结构化决策单，强制分开事实、推断、假设和分析判断，同时检查反证、替代解释、关键未知、权限边界、研究问题、放弃条件、阶段预算和机制压力测试。新决策单还应填写标准化 `decision_reason`（如 `policy_covered`、`insufficient_evidence`、`no_local_authority`、`mechanism_not_viable` 或 `reframe_required`）；旧决策单缺失该字段仍可读取，但停止原因会标为未分类。有效预研会自动回写对应制度新意审计，人工填写的复核标签不会被覆盖。分析结论为 `proceed` 或 `reframe` 仍停在人工闸门；只有 `pre-research-review --decision proceed` 才能进入深研，阻断性关键未知不能人工越过。
- 正式起草：深研仍由人工与Codex协作完成。固定结构 Markdown 必须先取得预研人工放行，再经 `evidence-check` 放行，才可使用 `sqmy draft` 确定性导出送审 DOCX；它不会调用模型。改稿覆盖同名送审文件时，旧导出任务会标记为 `skipped`，缓存只有在文件 SHA-256 与任务记录一致时才复用，避免旧输入哈希误指向新版文件。
- 审核和报送：`sqmy approve` 仅记录人工通过并复制到 `outputs/submission/`，不会发送材料；只有人工实际报送后才能执行 `mark-submitted`。
- mock 隔离：`generate-mock` 只用于测试，真实流程没有名为 `generate` 或 `export` 的模糊命令。
- 暂停/恢复：`sqmy pause RUN_ID --quota` 保存原阶段和下一动作，`sqmy resume RUN_ID` 只恢复暂停或失败运行。发现阶段通过 `sqmy scan --resume RUN_ID` 使用同一运行ID继续。
- 失败继续：`sqmy retry RUN_ID`。
- 提供商诊断：`sqmy provider-check` 验证Codex主通道；加 `--simulate-codex-quota` 时模拟额度错误并真实验证DeepSeek备用，同时写入审计库。
- 正式 DOCX 模板：`templates/submission_template.docx`。
- DOCX 字体与渲染：导出器对样式和文本 run 均显式写入等线；macOS 下使用 Word 私有等线字体进行 LibreOffice QA 的方法见 `docs/template_artifact.md`。
- mock 稿：`outputs/review/RUN_ID/`，不会自动进入 `outputs/submission/`，也不会自动发送。
- 证据审查：`evidence-import` 导入轻量“主张—来源”包，`evidence-check` 核对独立来源链、直接反证和现有政策覆盖。主张还需标注 `verified_fact`、`evidence_based_inference`、`unverified_hypothesis` 或 `analyst_judgment`，以及置信度、截至日期、不确定性和必要的可证伪条件；核心统计必须保存时间、地域、总体、单位和定义。未分类主张、假设、分析判断、过期核心来源和未解决冲突不能进入正文。没有主张、没有核心主张或存在阻断核心主张时均采用 fail-closed。
- 制度新意快审：模型初筛时同步产生一句可被反证的缺口假设、缺口类型和最多2条定向检索词。程序先查本地政策机制库，再做标题和摘要级反证检索。区级 `site:` 检索会保留本区查询并追加市级及中央官方范围查询；反证政策使用独立的730天窗口，不受普通候选90天窗口限制。只有“制度不存在”且被已核验机制直接覆盖时才自动阻断；执行、效果、协同和问责类缺口只标记复核并扣分，不自动删除。

## 安全和成本

密钥只放 `.env`，不得提交。默认 `provider = "auto"`：先通过本机已登录的 `codex exec` 使用ChatGPT/Codex订阅额度。纯元数据初筛在空临时工作目录中执行，并明确禁止工具、文件和网络访问，避免把仓库上下文重复计入Token。只有返回明确的额度或速率错误时，才尝试DeepSeek API。普通网络错误、结构化结果错误和质量错误不会触发切换。未设置 `DEEPSEEK_API_KEY` 时，额度错误会安全进入 `paused_quota`。

DeepSeek密钥可通过终端环境变量或本地 `.env` 提供；`.env` 已被Git忽略。调用记录保存提供商、模型、提示哈希、可获得的Token和估算成本。Codex订阅通道成本记为0元；DeepSeek按配置单价估算。

当前初筛显式使用 `screening_model`，与深研和诊断模型分开配置。备用模型固定为 `deepseek-v4-pro`，显式启用思考模式并设置 `reasoning_effort = "max"`；价格变化时应同步更新配置。

`sqmy budget` 直接按 `model_calls.created_at` 统计真实最近7天的全部模型调用，不再使用“最近10次运行”代替周预算，并显示预算调整复盘。缓存检查先于新调用预算预留；确需初筛时同时核对单次阶段上限、发现阶段7日上限、项目7日总上限和研究写作保护性预留。当前默认发现阶段7日上限为100000 Token，并从240000 Token总额度中为后续研究写作保护90000 Token。Codex CLI估算包含可配置的固定上下文安全垫和保守系数。单次在途调用无法可靠中断；若实际 Token 超出配置预算，系统会保存已取得结果、记录估算值和超限原因。未启用已开始任务完成策略时进入 `paused_budget`；启用后则只完成当前有界步骤，并阻止自动扩展。

预算的主要作用是在新题或新阶段启动前防止任务扩张。人工已发起的单个有界步骤，如果只是突破周额度或发现阶段累计额度，系统先完成并保存该步骤，再记录超额原因，提示下一个新模型任务前提额或等待释放；不因此自动扩展到备选题或下一阶段。单次阶段上限仍是防止异常输入和任务蔓延的硬边界；真实订阅或 API 额度耗尽仍安全暂停。

预算提高不是默认动作。使用 `budget-adjust` 记录原额度、新额度、原因和预期收益，任务完成后再用 `budget-review` 补记实际Token、实际收益以及保留、回退或继续评估的结论。这些命令只写审计记录，不会自动改写 `config/settings.toml`。

来源配置位于 `config/sources.toml`。搜索RSS只承担发现功能，记录中保存的是还原后的原始页面URL；候选阶段不批量下载网页全文。

投诉、论坛和社交平台不能依靠新闻RSS稳定覆盖。需要补充时，由 Codex 使用当前网页检索能力为本次扫描找到最多12条公开线索，按 `docs/clue_input.example.jsonl` 格式保存，再运行 `sqmy scan --clues PATH`。程序只保存标题、公开URL、发布日期和不超过700字的摘要，拒绝含Token、Cookie或会话参数的URL，并将规范化输入快照保存到本次运行目录以便恢复。

线索文件只放具备具体群体、具体场景和可反证制度缺口的候选形态事件。仅列出多个宽泛类别的热线月报、投诉总量或行业汇总不单独占用模型名额；它们应并入具体线索摘要作为规模或交叉验证材料，待有限预研再核对口径。

三级线索在模型前12个名额中最多保留2个核验名额；若无有价值线索，名额自动回填给一、二级信源。三级线索必须在有限预研中找到独立高等级来源；否则停止，不得进入正式稿。

发现层可观测参数位于 `[observability]`，影子核验参数位于 `[shadow_verification]`。每次完成扫描后会自动更新 `outputs/review/discovery_observability_rolling.json`；该报告只统计真实 `live` 运行，同时达到配置的最少运行数和至少14天观察跨度后才标记为可比较，同一天重跑不能冒充2—3周验证。它只用于判断不同影子建议与后续入选的相关性，不自动修改规则或开启阻断。

每个来源通过 `expansion_tier` 标记层级：1为海淀、北京主题源，2为北京新增权威源，3为全国权威与调查源，4为三级痛点线索。全国性议题不强制拥有北京落点，但须说明有权执行主体、地方试点可能或向上反映路径。

证据规则位于 `config/settings.toml` 的 `[evidence]`。多家媒体转述同一发布会、报告或数据源时只计为一个原始信息链；一个正式一级原始来源可以单独支撑其直接公布的事实。企业自报默认只证明“已公开宣称某机制”，不证明实际效果。

已核验的制度机制位于 `config/policy_mechanisms.toml`，每条都必须保存来源和最后核验时间。制度库自动匹配除满足关键词数量外，还必须命中至少一个主题特异词，只有“平台、投诉、举报”等通用词重合时不得判为已有政策覆盖。默认超过180天未重新核验的机制不再用于自动阻断，只作为复核线索。反证搜索会自动限定政府、法院和网信等一级来源；模型给出区级 `site:` 检索词时，同时保留本区查询并追加市级及中央官方范围查询。反证政策默认回看730天，避免把仍可能有效但早于普通90天新闻窗口的上位政策漏掉；命中仍只作为覆盖警告，是否现行有效须在预研中核验。程序会排除候选新闻自身和同标题页面。每次扫描完成后会自动重新生成 `outputs/review/metrics/novelty_rolling_21d.json`。报告一方面滚动统计审计量、阻断量、自动预研反馈、早期漏判、新增及重新开放事件、待处理队列、审计Token、模型前排除量、扩展层级、缓存命中和节省Token；另一方面按发现运行所在自然周归集“候选—选择—预研—深研—证据闸门—成稿—通过—报送”漏斗、停止原因及项目已记录Token。稳定性只使用已经结束的自然周：连续4个完整自然周每周至少1篇通过证据闸门的送审稿，才标记 `stable_minimum_output=true`；2篇目标单独统计。当前周单列，不会因某天没有新候选而提前判定失败。其中“潜在浪费防止量”是代理指标，交互式订阅Token若无法计量也会明确说明，不得当作实际账单节省。

## 测试

```bash
uv run --python 3.12 --with python-docx --with pytest python -m pytest -q
```

详细设计见 `docs/architecture.md`，模板证据见 `docs/template_artifact.md`。
