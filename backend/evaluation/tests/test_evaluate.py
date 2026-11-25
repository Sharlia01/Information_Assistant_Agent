import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SCRIPT = Path(__file__).resolve().parents[1] / "evaluate.py"
spec = importlib.util.spec_from_file_location("evaluate", SCRIPT)
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset = ev.read(SCRIPT.parent / "pilot_dataset.json")

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=15)

    def test_dataset_rejects_duplicate_and_bad_references(self):
        ev.validate_dataset(self.dataset)
        self.dataset["cases"][1]["id"] = self.dataset["cases"][0]["id"]
        with self.assertRaises(ValueError):
            ev.validate_dataset(self.dataset)
        self.dataset["cases"].pop(1)
        self.dataset["cases"][0]["reference"]["requirements"][0]["fact_ids"] = ["unknown"]
        with self.assertRaises(ValueError):
            ev.validate_dataset(self.dataset)

    def test_sse_multiline_comments_and_truncation(self):
        lines = [": heartbeat\r\n", "data: {\r\n", 'data: "type": "phase"}\r\n', "\r\n", "data: [DONE]\n", "\n"]
        self.assertEqual(list(ev.parse_sse(lines)), [{"type": "phase"}])
        with self.assertRaises(ValueError):
            list(ev.parse_sse(['data: {"type":"phase"}\n']))

    def complete_review(self, case, sha):
        review = ev.review_template(case, sha)
        review.update(status="complete", reviewer="test-human")
        review["audits"] = dict.fromkeys(review["audits"], True)
        for group in ("requirements", "facts", "unanswerable_targets"):
            for item in review[group]:
                item.update(report_location="测试段落", evidence=["fixture evidence"], reason="test annotation")
                item["score" if group == "requirements" else "status"] = 2 if group == "requirements" else "correct"
        return review

    def test_review_missing_facts_and_unverifiable_not_removed(self):
        case = self.dataset["cases"][0]
        review = self.complete_review(case, "sha")
        self.assertEqual(ev.validate_review(case, review, "sha"), [])
        review["facts"][0]["status"] = "missing"
        review["facts"][1]["status"] = "unverifiable"
        score = ev.score(review)
        n = len(review["facts"])
        self.assertEqual(score["key_fact_coverage"], (n - 2) / n)
        self.assertEqual(score["answered_key_fact_accuracy"], (n - 2) / (n - 1))
        self.assertIsNone(score["citation_support"])
        self.assertTrue(ev.validate_review(case, review, "changed"))
        review["facts"].pop()
        self.assertTrue(ev.validate_review(case, review, "sha"))

    def test_citations_count_uncited_important_claims(self):
        case = self.dataset["cases"][0]
        review = self.complete_review(case, "sha")
        for cited, status in [(True, "supports"), (True, "unverifiable"), (False, "missing")]:
            review["citations"].append({"claim": status, "cited": cited, "important": True, "status": status,
                                        "report_location": "section 1", "reason": "checked", "evidence": ["source"]})
        self.assertEqual(ev.validate_review(case, review, "sha"), [])
        self.assertEqual(ev.score(review)["citation_support"], .5)
        self.assertEqual(ev.score(review)["citation_coverage"], 1 / 3)

    def test_partial_event_on_timeout_does_not_destroy_result(self):
        (self.root / "events.jsonl").write_text('{"type":"phase"}\n{"type":', encoding="utf-8")
        result = ev.collect_result(self.root, None, True, 1)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(len(result["errors"]), 1)

    def test_malformed_review_returns_validation_errors(self):
        case = self.dataset["cases"][0]
        review = self.complete_review(case, "sha")
        review["facts"][0]["status"] = []
        self.assertTrue(ev.validate_review(case, review, "sha"))
        review["facts"] = None
        self.assertTrue(ev.validate_review(case, review, "sha"))

    def serve(self, behavior):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if behavior == "timeout":
                    time.sleep(2)
                    return
                events = [{"type": "phase", "phase": "planning"}]
                if behavior == "error":
                    events.append({"type": "error", "content": "fixture failure"})
                events.append({"type": "research_complete", "final_report": "# 测试报告\n仅用于离线测试。", "review_passed": False,
                               "completion_reason": "max_iterations", "references": []})
                for event in events:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/research/stream", requests

    def test_full_http_run_manual_review_summary_and_tamper(self):
        endpoint, requests = self.serve("success")
        root = self.root / "run"
        result = self.cli("run", "--cases", "pilot_01,pilot_05", "--endpoint", endpoint, "--output", root, "--timeout", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 1)
        self.assertEqual(set(requests[0]), {"query", "version", "search_modes"})
        self.assertEqual(requests[0]["query"], self.dataset["cases"][0]["query"])
        summary = ev.read(root / "summary.json")
        self.assertEqual(summary["counts"]["not_run"], 1)
        self.assertEqual(summary["completion_rate_all_selected"], .5)
        self.assertEqual(summary["reviewed_cases"], 0)
        self.assertEqual(self.cli("review-check", root).returncode, 1)
        directory = root / "pilot_01"
        review = self.complete_review(self.dataset["cases"][0], ev.digest(directory / "report.md"))
        ev.write(directory / "review.json", review)
        self.assertEqual(self.cli("review-check", root).returncode, 0)
        self.assertEqual(ev.read(root / "summary.json")["reviewed_cases"], 1)
        (directory / "report.md").write_text("tampered")
        self.assertEqual(self.cli("review-check", root).returncode, 1)
        self.assertNotEqual(self.cli("run", "--output", root).returncode, 0)

    def test_server_error_not_counted_as_success(self):
        endpoint, _ = self.serve("error")
        root = self.root / "error"
        result = self.cli("run", "--cases", "pilot_01", "--endpoint", endpoint, "--output", root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ev.read(root / "summary.json")["counts"]["failed"], 1)

    def test_timeout_stops_batch_and_keeps_denominator(self):
        endpoint, requests = self.serve("timeout")
        root = self.root / "timeout"
        result = self.cli("run", "--cases", "pilot_01,pilot_02", "--endpoint", endpoint, "--output", root, "--timeout", "0.7")
        self.assertEqual(result.returncode, 0, result.stderr)
        counts = ev.read(root / "summary.json")["counts"]
        self.assertEqual(counts["timed_out"], 1)
        self.assertEqual(counts["not_run"], 1)
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
