"""离线流程回归测试：所有业务数据内嵌，不请求模型、搜索或数据库。

运行（项目根目录）：
    python -m unittest discover -s backend/tests -p 'test_research_workflow.py' -v

测试按预期行为断言。发现的业务缺陷保持为失败，不使用 expectedFailure 隐藏。
"""

import asyncio
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from service.deep_research_v2 import graph as graph_module
from service.deep_research_v2.agents.critic import CriticMaster
from service.deep_research_v2.agents.writer import LeadWriter
from service.deep_research_v2.state import ResearchPhase, create_initial_state


def make_agent(cls):
    # 跳过创建 API 客户端，保留真实 process、提示词组装及状态更新。
    agent = object.__new__(cls)
    agent.name = cls.__name__
    agent.logger = logging.getLogger(f"test.{cls.__name__}")
    agent.call_llm = AsyncMock(side_effect=AssertionError("未设置固定模型返回"))
    return agent


def review(verdict="pass", search=False):
    return {
        "overall_assessment": {"verdict": verdict, "quality_score": 8},
        "issues": [] if verdict == "pass" else [{
            "issue_type": "missing_source" if search else "logic_error",
            "severity": "major", "description": "固定审核问题",
            "suggestion": "补充来源" if search else "修改表述",
            "requires_new_search": search,
            "search_query": "固定补搜关键词" if search else "",
        }],
        "missing_aspects": [],
    }


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.cancelled = False
        self.state = create_initial_state("固定研究问题", "offline-session")
        self.state["max_iterations"] = 3
        self.graph = object.__new__(graph_module.DeepResearchGraph)
        self.graph.checkpoint_service = Mock()
        self.graph.checkpoint_service.save_checkpoint.return_value = "checkpoint-id"
        self.graph.max_iterations = 3

        def plan(state):
            state["outline"] = [{"id": "A", "title": "章节A"}]

        def scout(state):
            if state["phase"] == ResearchPhase.RE_RESEARCHING.value:
                self.assertEqual(state["pending_search_queries"], ["固定补搜关键词"])
            state["facts"].append({
                "id": f"f{len(state['facts'])}", "content": "固定事实",
                "source_name": "固定来源", "source_url": "https://example.test/source",
                "related_sections": ["A"],
            })

        def write(state):
            state["final_report"] = f"报告V{len(self.writer_inputs)}"
            state["references"] = [{"id": 1, "source": "固定来源",
                                    "url": "https://example.test/source"}]
            state["phase"] = ResearchPhase.REVIEWING.value

        self.writer_inputs = []
        self.graph.architect = self.fake("architect", plan)
        self.graph.scout = self.fake("scout", scout)
        self.graph.data_analyst = self.fake("data_analyst")
        self.graph.wizard = self.fake("wizard")

        def record_write(state):
            self.writer_inputs.append((state["phase"], len(state["facts"])))
            write(state)

        self.graph.writer = self.fake("writer", record_write)
        # 使用真实 critic.process 和路由规则，只固定审核模型的 JSON。
        self.graph.critic = make_agent(CriticMaster)
        self.graph.critic.call_llm.return_value = json.dumps(review())
        self.graph.critic.call_llm.side_effect = None

    def fake(self, name, action=None):
        async def process(state):
            self.calls.append(name)
            # 唤醒真实流式队列，避免每个固定步骤等待 0.5 秒超时。
            state["_message_queue"].put_nowait({"type": "test_step", "agent": name})
            if action:
                action(state)
            return state
        return SimpleNamespace(name=name, process=AsyncMock(side_effect=process))

    async def run_flow(self):
        with patch.object(graph_module, "clear_cancel_flag"), patch.object(
            graph_module, "is_research_cancelled", side_effect=lambda _: self.cancelled
        ):
            async def collect():
                return [event async for event in self.graph._run_simplified(self.state)]
            events = await asyncio.wait_for(collect(), timeout=5)
        self.assertIsNone(self.state["_message_queue"])
        return events

    def complete_event(self, events):
        completed = [e for e in events if e["type"] == "research_complete"]
        self.assertEqual(len(completed), 1)
        self.assertNotIn("error", [e["type"] for e in events])
        return completed[0]

    async def test_normal_completion_and_reference_mapping(self):
        events = await self.run_flow()
        self.assertEqual(self.calls, ["architect", "scout", "data_analyst", "wizard", "writer"])
        self.assertEqual([e["phase"] for e in events if e["type"] == "phase"],
                         ["planning", "researching", "analyzing", "writing", "reviewing"])
        final = self.complete_event(events)
        self.assertEqual(final["final_report"], "报告V1")
        self.assertEqual(final["facts_count"], 1)
        self.assertEqual(final["charts_count"], 0)
        self.assertEqual(final["completion_reason"], "review_passed")
        self.assertTrue(final["review_passed"])
        self.assertEqual(final["unresolved_issues"], 0)
        self.assertNotIn("warning", [e["type"] for e in events])
        self.assertEqual(final["references"], [{"id": 1, "title": "固定来源",
            "link": "https://example.test/source", "content": "固定事实", "source": "web"}])
        self.graph.checkpoint_service.update_status.assert_called_once_with("offline-session", "completed")

    async def test_revision_then_review(self):
        self.graph.critic.call_llm.side_effect = [json.dumps(review("needs_revision")), json.dumps(review())]
        events = await self.run_flow()
        self.assertEqual(self.calls.count("scout"), 1)
        self.assertEqual(self.writer_inputs, [("writing", 1), ("revising", 1)])
        self.assertEqual(self.graph.critic.call_llm.await_count, 2)
        self.assertIn("报告V2", self.graph.critic.call_llm.call_args.kwargs["user_prompt"])
        self.assertEqual(self.complete_event(events)["final_report"], "报告V2")
        self.assertTrue(self.complete_event(events)["review_passed"])

    async def test_research_then_rewrite(self):
        self.graph.critic.call_llm.side_effect = [json.dumps(review("needs_revision", search=True)), json.dumps(review())]
        events = await self.run_flow()
        self.assertEqual(self.calls, ["architect", "scout", "data_analyst", "wizard", "writer", "scout", "writer"])
        self.assertEqual(self.writer_inputs, [("writing", 1), ("writing", 2)])
        self.assertEqual(self.complete_event(events)["facts_count"], 2)
        self.assertEqual(self.graph.critic.call_llm.await_count, 2)

    async def test_iteration_limit_terminates(self):
        self.state["max_iterations"] = 2
        self.graph.critic.call_llm.return_value = json.dumps(review("needs_revision"))
        await self.run_flow()
        self.assertEqual(self.graph.critic.call_llm.await_count, 2)
        self.assertEqual(self.state["iteration"], 2)
        self.assertGreater(self.state["unresolved_issues"], 0)

    async def test_iteration_limit_exposes_unresolved_status(self):
        self.state["max_iterations"] = 1
        self.graph.critic.call_llm.return_value = json.dumps(review("needs_revision"))
        events = await self.run_flow()
        final = self.complete_event(events)
        warnings = [e for e in events if e["type"] == "warning"]
        self.assertEqual(final["completion_reason"], "max_iterations")
        self.assertFalse(final["review_passed"])
        self.assertEqual(final["unresolved_issues"], 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("未再次审核", warnings[0]["content"])
        self.assertEqual(final["final_report"], "报告V2")
        self.assertEqual(self.graph.critic.call_llm.await_count, 1)

    async def test_empty_review_is_not_reported_as_passed(self):
        self.graph.critic.call_llm.return_value = "{}"
        self.state["review_passed"] = True  # 不能沿用旧审核结果。
        final = self.complete_event(await self.run_flow())
        self.assertFalse(final["review_passed"])
        self.assertEqual(final["completion_reason"], "review_incomplete")

    async def test_agent_failure_stops_workflow(self):
        def fail(_):
            raise RuntimeError("固定搜索失败")
        self.graph.scout = self.fake("scout", fail)
        events = await self.run_flow()
        self.assertEqual(self.calls, ["architect", "scout"], "失败后不应启动后续 Agent")
        self.assertNotIn("research_complete", [e["type"] for e in events])
        self.assertTrue(any(e["type"] == "error" and "固定搜索失败" in e["content"] for e in events))
        self.graph.checkpoint_service.update_status.assert_called_once_with("offline-session", "failed", "固定搜索失败")

    async def test_cancel_between_stages(self):
        self.graph.architect = self.fake("architect", lambda _: setattr(self, "cancelled", True))
        events = await self.run_flow()
        self.assertEqual(self.calls, ["architect"])
        self.assertEqual(sum(e["type"] == "research_cancelled" for e in events), 1)
        self.assertNotIn("research_complete", [e["type"] for e in events])

    async def test_cancel_running_agent_cleans_task(self):
        stopped = asyncio.Event()

        async def blocking(state):
            self.calls.append("scout")
            self.cancelled = True
            state["_message_queue"].put_nowait({"type": "test_step"})
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.graph.scout.process = AsyncMock(side_effect=blocking)
        events = await self.run_flow()
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.calls, ["architect", "scout"])
        self.assertEqual(sum(e["type"] == "research_cancelled" for e in events), 1)
        self.assertNotIn("research_complete", [e["type"] for e in events])


class AgentDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = create_initial_state("固定问题", "offline-agent")
        self.section = {"id": "B", "title": "章节B", "description": "固定描述"}
        self.state["outline"] = [{"id": "A", "title": "章节A"}, self.section]
        self.writer = make_agent(LeadWriter)
        self.writer.call_llm.side_effect = None
        self.writer.call_llm.return_value = json.dumps({"content": "章节B正文", "citations": [
            {"marker": "[1]", "source": "固定来源", "url": "https://example.test/b"}]})

    async def test_section_uses_all_its_facts_and_preserves_other_drafts(self):
        self.state["draft_sections"] = {"A": "保留A正文"}
        self.state["facts"] = [{"content": "A独有事实", "related_sections": ["A"]}] + [
            {"content": f"B事实{i:02d}", "related_sections": ["B"]} for i in range(12)]
        await self.writer._write_section(self.state, self.section)
        prompt = self.writer.call_llm.call_args.kwargs["user_prompt"]
        self.assertNotIn("A独有事实", prompt)
        for i in range(12):
            self.assertIn(f"B事实{i:02d}", prompt)
        self.assertEqual(self.state["draft_sections"], {"A": "保留A正文", "B": "章节B正文"})
        self.assertEqual(self.state["references"][0]["url"], "https://example.test/b")
        content_events = [m for m in self.state["messages"] if m["type"] == "section_content"]
        self.assertEqual(content_events[0]["content"]["section_id"], "B")

    async def test_section_fallback_uses_first_ten_facts(self):
        self.state["facts"] = [{"content": f"兜底事实{i:02d}", "related_sections": ["A"]} for i in range(12)]
        await self.writer._write_section(self.state, self.section)
        prompt = self.writer.call_llm.call_args.kwargs["user_prompt"]
        for i in range(10):
            self.assertIn(f"兜底事实{i:02d}", prompt)
        self.assertNotIn("兜底事实10", prompt)
        self.assertNotIn("兜底事实11", prompt)

    async def test_critic_reviews_final_report_instead_of_stale_drafts(self):
        self.state["draft_sections"] = {"A": "旧章节草稿标记"}
        self.state["final_report"] = "最新完整报告标记"
        critic = make_agent(CriticMaster)
        critic.call_llm.side_effect = None
        critic.call_llm.return_value = json.dumps(review())
        await critic._review_content(self.state)
        prompt = critic.call_llm.call_args.kwargs["user_prompt"]
        self.assertIn("最新完整报告标记", prompt)
        self.assertNotIn("旧章节草稿标记", prompt)

    async def test_revision_updates_report_for_next_review(self):
        self.state["draft_sections"] = {"A": "旧章节草稿标记"}
        self.state["phase"] = ResearchPhase.REVISING.value
        self.state["final_report"] = "报告V1"
        self.state["critic_feedback"] = [{"id": "issue-1", "severity": "major",
            "description": "固定问题", "suggestion": "修订", "resolved": False}]
        self.writer.call_llm.return_value = json.dumps({"revised_content": "报告V2", "addressed_issues": ["issue-1"]})
        await self.writer.process(self.state)
        self.assertEqual(self.state["final_report"], "报告V2")
        self.assertEqual(self.state["phase"], ResearchPhase.REVIEWING.value)
        self.assertTrue(self.state["critic_feedback"][0]["resolved"])
        critic = make_agent(CriticMaster)
        critic.call_llm.side_effect = None
        critic.call_llm.return_value = json.dumps(review())
        await critic.process(self.state)
        prompt = critic.call_llm.call_args.kwargs["user_prompt"]
        self.assertIn("报告V2", prompt)
        self.assertNotIn("报告V1", prompt)
        self.assertNotIn("旧章节草稿标记", prompt)

    async def test_critic_falls_back_to_drafts_when_report_is_empty(self):
        self.state["draft_sections"] = {"A": "章节A草稿", "B": "章节B草稿"}
        critic = make_agent(CriticMaster)
        critic.call_llm.side_effect = None
        critic.call_llm.return_value = json.dumps(review())
        for report in ("", " \n", None):
            with self.subTest(report=report):
                self.state["final_report"] = report
                await critic._review_content(self.state)
                prompt = critic.call_llm.call_args.kwargs["user_prompt"]
                self.assertIn("## 章节A\n章节A草稿", prompt)
                self.assertIn("## 章节B\n章节B草稿", prompt)


if __name__ == "__main__":
    unittest.main()
