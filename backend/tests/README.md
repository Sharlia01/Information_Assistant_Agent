# 研究流程离线测试

在项目根目录、已安装后端依赖的 Python 环境中运行：

```sh
python -m unittest discover -s backend/tests -p 'test_research_workflow.py' -v
```

本机可用环境：`/opt/miniconda3/envs/deepresearch/bin/python`。
无需配置真实 API Key。固定问题、事实、章节和模型 JSON 均内嵌在测试文件中。
测试不实例化真实 API 客户端，不执行生成代码，检查点服务使用 Mock。

## 范围

- 使用真实 `_run_simplified` 调度（当前 `run` 使用的流式路径），固定规划、搜索、分析及写作 Agent 的行为。
- 保留真实 `CriticMaster.process`、审核提示词组装、JSON 解析和路由规则，只替换模型调用。
- 单独使用真实 writer 方法验证章节事实筛选、草稿保存、引用及修订状态更新。
- 检查正常完成、修订、补搜、迭代上限、异常、阶段间取消和执行中取消。
- 通过真实消息队列收集事件，验证完成事件及清理行为。

不覆盖模型内容质量、真实搜索、真实数据库、浏览器渲染、检查点恢复和备用 LangGraph/run_sync 路径。

## 首次运行结果

2026-11-06：12 个测试，9 个通过，3 个失败。失败用例是预期行为的回归断言，未跳过或标为 expectedFailure；整套命令当前返回非零退出码。

| 失败用例 | 发现的问题 |
| --- | --- |
| `test_critic_reviews_final_report_instead_of_stale_drafts` | 审核优先读取章节草稿，未读取已生成的最终报告。 |
| `test_agent_failure_stops_workflow` | 流式包装捕获 Agent 异常后只记录日志，仍启动后续 Agent。 |
| `test_iteration_limit_exposes_unresolved_status` | 迭代到上限仍有未解决问题时，既未发出上限 warning，也未在完成事件中暴露未解决问题数量。 |

本次仅新增测试，不修改业务实现。修复时应继续运行完整测试集，确保这些断言通过且已有路径不退化。

## 异常传播修复后

2026-11-06：流式包装函数记录 Agent 异常后继续抛出，由外层标记检查点失败、发出错误事件并清理队列。重新运行 12 个测试，10 个通过，2 个失败。
`test_agent_failure_stops_workflow` 已通过，验证失败后不启动后续 Agent、不发出成功事件，且检查点被标为失败。
剩余失败仍为旧草稿审核和迭代上限提示问题，尚未修改这两处行为。

## 迭代上限提示修复后

2026-11-08：13 个测试，12 个通过，1 个失败（旧草稿审核）。
完成事件新增 `completion_reason`（`review_passed` / `max_iterations` / `review_incomplete`）、`review_passed` 和 `unresolved_issues`。
达到上限时额外发送 `warning`；评分和严重问题数沿用最近一次审核，未复核的修订不会被认定为已通过。
补充测试验证正常通过、修订后通过、上限结束和空审核结果；原有迭代次数及最后一次修订行为保持不变。
这些字段及 warning 是后端事件契约，本次未修改前端展示。

## 审核对象修复后

2026-11-08：14 个测试全部通过。审核优先使用最新 `final_report`，仅在报告缺失、为空或只有空白时使用章节草稿。
测试覆盖旧草稿与最终报告同时存在、真实 writer 修订后进入 critic，以及空报告的草稿兜底。
当前仍保留整体前 8000 字符、前 20 条事实和前 15 个数据点的审核输入限制。
