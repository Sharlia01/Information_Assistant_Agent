# 研究质量评测

当前提供 **真实 HTTP 执行 → 原始结果保存 → 人工内容评分 → 评分校验 → 指标汇总** 的最小闭环。仅依赖 Python 3.10+ 标准库，不需要安装评测框架。

它与 `backend/tests` 的流程回归测试互补。系统内部 Critic 的分数只存档，不参与外部质量评分。本版本没有自动模型评审、固定搜索回放、版本显著性检验，也不自动抓取引用全文。

## 1. 校验并运行

在项目根目录执行：

```sh
python3 backend/evaluation/evaluate.py validate

# 先启动真实后端及其依赖，再运行。会调用模型、搜索服务并产生费用。
python3 backend/evaluation/evaluate.py run \
  --output backend/evaluation/runs/pilot-v1 \
  --timeout 1800 \
  --configuration-note '记录实际服务器模型、提示词版本、迭代上限和部署版本'

# 可选择少量题目；输出目录必须是新目录，避免覆盖历史结果。
python3 backend/evaluation/evaluate.py run \
  --cases pilot_01,pilot_02 \
  --output backend/evaluation/runs/pilot-v2
```

默认入口为 `http://127.0.0.1:8000/research/stream`，可用 `--endpoint` 指定。若服务需要 Bearer 认证，用环境变量 `EVAL_BEARER_TOKEN` 提供，不要将凭据写入 URL 或配置备注。

执行器只发送 `query`、V2 版本和 web 搜索模式，不发送 `reference`、标准事实或评分规则。历史时间范围沿用题目约束，**不保证搜索服务执行日期过滤**，评审必须核对证据发布日期。

`provided_material_only` 当前明确记为 `not_run`，不发送请求：现有接口没有受控材料注入及工具隔离保证。不能偷偷改为开放搜索题后继续评分。后续实现材料适配器后才能运行第 5 题。

每题的超时是客户端总时限。超时会停止当前采集及后续批次，不自动重试；后端任务可能仍在运行，应确认结束或通过产品取消后再启动下一批。失败和超时均保留记录。

当前 HTTP 接口未返回实际模型配置、token 和费用，这些字段保留 `null`，不能解释为零。请求里的迭代上限在当前 V2 路径未传递，执行器不提供一个会造成误解的 `--max-iterations` 参数；请在服务器配置并记录。

## 2. 查看结果

```text
pilot-v1/
  dataset.json          # 本次数据集完整快照，仅供外部评分
  manifest.json         # 数据集哈希、时间、git revision、工作区是否有修改、配置备注
  pilot_01/
    request.json        # 实际请求，无标准答案
    events.jsonl        # 收到的完整 JSON 事件，逐事件落盘
    transport.log       # HTTP/解析错误
    report.md           # 完成事件中的最终报告
    references.json     # 返回的引用
    result.json         # 执行状态、耗时、内部审核结果、报告哈希
    review.json         # 待人工填写的外部评分
  ...
  summary.json
  summary.md
```

仅收到 `[DONE]` 不算成功：必须无传输/解析错误、无 error 事件，且恰有一个带非空报告的 `research_complete`。`review_passed=false` 不改变执行完成状态，内容是否合格由外部评分决定。

事件日志只保存服务器实际输出的内容，不保证包含全部内部状态或证据原文。引用列表不是网页快照。评审时应在每题目录下另存 `evidence/` 文件，记录所核查原文、URL、发布日期、抓取日期和页码/段落；按来源授权保存允许保留的内容。评测产物可能包含业务信息，默认忽略 `runs/`，不要直接提交。

## 3. 填写人工评分

读取 `dataset.json` 中该题 `reference` 的评分锚点，对照 `report.md` 和原始证据填写 `review.json`：

- `reviewer`：评审者标识；所有项完成后把 `status` 从 `pending` 改成 `complete`。
- `requirements`：按每条要求的锚点填 `score: 0/1/2`。
- `facts`：必须逐项评所有 required 事实，填 `correct/incorrect/missing/unverifiable`。对象、时间、口径、单位、数值全部正确才是 correct。换算到参考单位后检查绝对容差；派生事实人工独立复算。此版本不会从自由文本自动提取和判断数值。
- `unanswerable_targets`：填 `correct/incorrect/missing`，只对材料受限题适用。
- 每项填写 `report_location`、`reason`、`evidence`。证据是字符串数组，可填 `S03: 二、1—12月`、新来源链接或 `evidence/xxx.txt: 段落2`。缺失内容位置写 `未出现`，missing 可用空证据数组。程序检查记录完整性，不证明证据本身有效。
- 四个 `audits` 均需人工检查完整报告后设为 `true`。空列表表示确认没有相应项，不表示未检查；尚未检查必须保留 false。
- 不修改 `case_id` 或 `report_sha256`。报告变化后旧评分作废，应另建运行记录。

单条事实示例：

```json
{
  "id": "F2024_sales",
  "status": "correct",
  "report_location": "第二节表格，2024年销量行",
  "evidence": ["S03: 二、1—12月新能源汽车段"],
  "reason": "对象、年度、销量口径及单位一致，数值在参考容差内。"
}
```

`citations` 按**可核查的原子论断**记录：遍历所有重要论断（包括没有引用的），另外纳入其他有引用的实质论断。同一论断有多条引用时，作为一组联合判断，不通过堆引用增加分母。同一个论断只登记一次。

```json
{
  "claim": "报告中的具体可核查论断",
  "important": true,
  "cited": true,
  "status": "supports",
  "report_location": "第二节第一段",
  "evidence": ["evidence/source-03.txt: 段落5"],
  "reason": "来源原文支持完整论断及其范围。"
}
```

支持状态为 `supports/partially_supports/does_not_support/unverifiable/missing`；没有引用必须 `cited:false,status:missing`。打不开或没有保留原文时应使用 unverifiable，而不是猜测支持。

`extra_claims` 记录关键事实列表以外的实质论断，字段为 `claim,status,report_location,evidence,reason`；状态为 `supported/unsupported/unverifiable/explicit_inference`。已明确说明且合理的推断不直接算错。

`charts` 每个图表一项，字段为 `id,status,report_location,evidence,reason`；状态为 `correct/incorrect/unverifiable`。核对图中数据、标签、时间、单位与正文；未生成任何图表时可以是空列表。题目要求但缺失的图表在 requirements 扣分。

`critical_errors` 每个严重错误一项，字段为 `type,report_location,evidence,reason`，参照题目严重错误定义并包含新发现的问题。没有错误填写空列表。

首批标注和争议项建议第二位评审者复核，在 notes 中留复核记录。源数据集当前 `human_review_status:pending` 仍需单独完成参考答案核验；一份报告被评分不会自动批准参考标注。

## 4. 校验评分并汇总

```sh
python3 backend/evaluation/evaluate.py review-check backend/evaluation/runs/pilot-v1
python3 backend/evaluation/evaluate.py summarize backend/evaluation/runs/pilot-v1
```

`review-check` 对已完成执行的题目检查评分是否齐全、枚举值/ID 是否正确、报告哈希是否匹配。有未完成或无效评分返回非零退出码；失败和未运行题不要求内容评分。命令同时生成汇总。

`summary.json` 包含逐题指标、校验错误，以及仅对有效已评分题目计算的宏平均和每项有效题数：

- 要求覆盖：要求得分总和 / (2 × 要求数)。
- 关键事实覆盖：正确必答事实 / 全部必答事实，遗漏不剔除。
- 已答关键事实准确率：正确 / 非 missing 的必答事实。unverifiable 不当成正确。
- 引用支持率：完全支持的已引用论断 / 全部已引用论断。
- 引用覆盖率：有充分支持的重要论断 / 全部重要论断。
- 材料不足处理正确率、额外不支持论断数、无法核实事实数、严重错误数。

零分母输出 `null`，不是 0 或 100%。未评分题不计零分也不进入内容平均，但会明确显示已评分数量。执行完成率同时给出“全部选中题目”和“实际尝试题目”两个分母，失败/超时不删除。没有综合总分或默认合格阈值，严重错误门槛通过也不代表整体合格。

比较版本时使用不同输出目录、同一数据集版本与题目集合、相同预算，检查实际配置备注；实时搜索会造成波动。5 道试评题用于发现问题和校准评分，不支持总体准确率结论。

## 离线验证

```sh
python3 -m unittest discover -s backend/evaluation/tests -v
```

测试使用本机模拟 SSE 服务，不调用真实模型、搜索或数据库，不产生模型费用。
