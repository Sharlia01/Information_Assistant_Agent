"""Standard-library evaluation runner. See README.md for the review protocol."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
FACT_STATUSES = {"correct", "incorrect", "missing", "unverifiable"}
CITATION_STATUSES = {"supports", "partially_supports", "does_not_support", "unverifiable", "missing"}
EXTRA_STATUSES = {"supported", "unsupported", "unverifiable", "explicit_inference"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_dataset(data):
    cases = data.get("cases", [])
    if not cases or not data.get("dataset_id"):
        raise ValueError("dataset_id and non-empty cases are required")
    ids = set()
    for case in cases:
        cid = case["id"]
        if not isinstance(cid, str) or not cid or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in cid):
            raise ValueError("case id must be a safe directory name")
        if cid in ids:
            raise ValueError(f"duplicate case id: {cid}")
        ids.add(cid)
        if not case.get("query") or case.get("mode") not in {"open_web", "provided_material_only"}:
            raise ValueError(f"{cid}: invalid query/mode")
        if case["mode"] == "provided_material_only" and not case.get("materials"):
            raise ValueError(f"{cid}: materials required")
        ref = case["reference"]
        for group in ("requirements", "facts", "sources", "unanswerable_targets"):
            values = ref.get(group, [])
            group_ids = [item["id"] for item in values]
            if len(group_ids) != len(set(group_ids)):
                raise ValueError(f"{cid}: duplicate {group} ids")
        if not ref.get("requirements") or not ref.get("facts"):
            raise ValueError(f"{cid}: requirements/facts required")
        fact_ids = {f["id"] for f in ref["facts"]}
        source_ids = {s["id"] for s in ref.get("sources", [])}
        for req in ref["requirements"]:
            if not set(req.get("fact_ids", [])) <= fact_ids:
                raise ValueError(f"{cid}: unknown requirement fact")
        for fact in ref["facts"]:
            if not set(fact.get("input_fact_ids", [])) <= fact_ids:
                raise ValueError(f"{cid}: unknown derived input")
            if any(e["source_id"] not in source_ids for e in fact.get("evidence", [])):
                raise ValueError(f"{cid}: unknown evidence source")


def annotation(item_id, field="status"):
    return {"id": item_id, field: None, "report_location": "", "evidence": [], "reason": ""}


def review_template(case, report_hash):
    ref = case["reference"]
    return {
        "case_id": case["id"], "report_sha256": report_hash,
        "reviewer": "", "status": "pending",
        "requirements": [annotation(r["id"], "score") for r in ref["requirements"]],
        "facts": [annotation(f["id"]) for f in ref["facts"] if f.get("required", True)],
        "unanswerable_targets": [annotation(u["id"]) for u in ref.get("unanswerable_targets", [])],
        "audits": {k: False for k in ("citations", "extra_claims", "charts", "critical_errors")},
        "citations": [], "extra_claims": [], "charts": [], "critical_errors": [],
        "notes": "",
    }


def parse_sse(lines):
    """Parse complete SSE frames, including CRLF and multiline data."""
    parts = []
    for line in lines:
        line = line.rstrip("\r\n")
        if not line:
            if parts:
                payload = "\n".join(parts)
                parts = []
                if payload == "[DONE]":
                    return
                event = json.loads(payload)
                if not isinstance(event, dict):
                    raise ValueError("SSE data must be an object")
                yield event
        elif line.startswith("data:"):
            parts.append(line[5:].removeprefix(" "))
    if parts:
        raise ValueError("truncated SSE frame")


def worker(args):
    """Isolated process lets the parent enforce a wall-clock deadline."""
    case_dir = Path(args.directory)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    token = os.environ.get("EVAL_BEARER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(args.endpoint, data=(case_dir / "request.json").read_bytes(), headers=headers)
    with urlopen(request, timeout=args.timeout) as response:
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raise ValueError("expected text/event-stream response")
        with (case_dir / "events.jsonl").open("w", encoding="utf-8") as output:
            for event in parse_sse(line.decode("utf-8") for line in response):
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
                output.flush()


def collect_result(directory, returncode, timed_out, elapsed):
    events_path = directory / "events.jsonl"
    events = []
    parse_errors = []
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                parse_errors.append({"type": "error", "content": "Incomplete or invalid persisted event; inspect events.jsonl"})
    completions = [e for e in events if e.get("type") == "research_complete"]
    errors = [e for e in events if e.get("type") == "error"] + parse_errors
    final = completions[-1] if completions else {}
    report = final.get("final_report", "")
    if not isinstance(report, str):
        report = ""
    status = "timed_out" if timed_out else "completed" if returncode == 0 and len(completions) == 1 and report.strip() and not errors else "failed"
    (directory / "report.md").write_text(report, encoding="utf-8")
    write(directory / "references.json", final.get("references", []))
    result = {
        "status": status, "elapsed_seconds": round(elapsed, 3), "worker_returncode": returncode,
        "completion_reason": final.get("completion_reason"),
        "internal_review_passed": final.get("review_passed"),
        "internal_quality_score": final.get("quality_score"),
        "unresolved_issues": final.get("unresolved_issues"), "errors": errors,
        "report_sha256": digest(directory / "report.md"),
        "token_usage": None, "cost": None,
        "actual_model_configuration": None,
    }
    write(directory / "result.json", result)
    return result


def run(args):
    dataset = read(args.dataset)
    validate_dataset(dataset)
    selected = args.cases.split(",") if args.cases else [c["id"] for c in dataset["cases"]]
    if not set(selected) <= {c["id"] for c in dataset["cases"]}:
        raise ValueError("unknown case id")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=False)
    write(root / "dataset.json", dataset)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=HERE, capture_output=True, text=True)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(), "dataset_id": dataset["dataset_id"],
        "dataset_sha256": digest(root / "dataset.json"), "cases": selected,
        "endpoint": args.endpoint, "timeout_seconds": args.timeout,
        "mode": "live_web", "git_revision": revision.stdout.strip(),
        "working_tree_dirty": bool(dirty.stdout.strip()),
        "configuration_note": args.configuration_note,
        "limitations": ["HTTP API does not expose effective model config, token usage or cost; null means unknown.",
                        "Source text is preserved only when emitted by the server; references alone are not evidence snapshots.",
                        "Client timeout terminates collection, not necessarily backend execution. No automatic retries."],
    }
    write(root / "manifest.json", manifest)
    for case in dataset["cases"]:
        if case["id"] not in selected:
            continue
        directory = root / case["id"]
        directory.mkdir()
        # Never pass reference answers, evidence cards or rubrics to the system.
        if case["mode"] != "open_web":
            write(directory / "result.json", {
                "status": "not_run", "reason": "Current HTTP adapter cannot guarantee materials-only execution; no request sent."
            })
            print(f"{case['id']}: not_run (materials-only adapter unavailable)", flush=True)
            continue
        payload = {"query": case["query"], "version": "v2", "search_modes": ["web"]}
        write(directory / "request.json", payload)
        started = time.monotonic()
        timed_out = False
        with (directory / "transport.log").open("w", encoding="utf-8") as log:
            try:
                process = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "_worker", "--directory", str(directory.resolve()),
                     "--endpoint", args.endpoint, "--timeout", str(args.timeout)],
                    stdout=log, stderr=log, timeout=args.timeout,
                )
                returncode = process.returncode
            except subprocess.TimeoutExpired:
                timed_out, returncode = True, None
        result = collect_result(directory, returncode, timed_out, time.monotonic() - started)
        write(directory / "review.json", review_template(case, result["report_sha256"]))
        print(f"{case['id']}: {result['status']}", flush=True)
        if timed_out:
            # Avoid overlapping expensive backend jobs after a client-side timeout.
            for remaining in dataset["cases"]:
                target = root / remaining["id"]
                if remaining["id"] in selected and not target.exists():
                    target.mkdir()
                    write(target / "result.json", {"status": "not_run", "reason": "Batch stopped after timeout; backend may still be running."})
            break
    summarize(root)


def validate_review(case, review, report_hash):
    errors = []
    if not isinstance(review, dict):
        return ["review must be an object"]
    for group in ("requirements", "facts", "unanswerable_targets", "citations", "extra_claims", "charts", "critical_errors"):
        items = review.get(group)
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            errors.append(f"{group}: array of objects required")
    if not isinstance(review.get("audits"), dict):
        errors.append("audits object required")
    if errors:
        return errors
    if review.get("case_id") != case["id"] or review.get("report_sha256") != report_hash:
        errors.append("review case/report hash mismatch")
    if review.get("status") != "complete":
        return errors + ["review is pending"]
    if not str(review.get("reviewer", "")).strip():
        errors.append("reviewer required")

    def details(item, name, evidence_required=True):
        if not item.get("report_location") or not item.get("reason"):
            errors.append(f"{name}: report_location and reason required")
        if not isinstance(item.get("evidence"), list) or any(not isinstance(e, str) or not e.strip() for e in item.get("evidence", [])) or (evidence_required and not item["evidence"]):
            errors.append(f"{name}: evidence list required")

    ref = case["reference"]
    groups = [
        ("requirements", ref["requirements"], "score", {0, 1, 2}),
        ("facts", [f for f in ref["facts"] if f.get("required", True)], "status", FACT_STATUSES),
        ("unanswerable_targets", ref.get("unanswerable_targets", []), "status", {"correct", "incorrect", "missing"}),
    ]
    for group, expected, field, allowed in groups:
        items = review.get(group, [])
        item_ids = [i.get("id") for i in items]
        if any(not isinstance(i, str) for i in item_ids):
            errors.append(f"{group}: string IDs required")
            continue
        if len(item_ids) != len(set(item_ids)) or set(item_ids) != {i["id"] for i in expected}:
            errors.append(f"{group}: expected exact unique IDs")
        for item in items:
            value = item.get(field)
            if not isinstance(value, (str, int, float)) or isinstance(value, bool) or value not in allowed:
                errors.append(f"{group}/{item.get('id')}: invalid {field}")
            details(item, group, value != "missing")
    for group, allowed in (("citations", CITATION_STATUSES), ("extra_claims", EXTRA_STATUSES),
                           ("charts", {"correct", "incorrect", "unverifiable"})):
        if review.get("audits", {}).get(group) is not True:
            errors.append(f"{group}: full-report audit not confirmed")
        for item in review.get(group, []):
            if not isinstance(item.get("status"), str) or item["status"] not in allowed:
                errors.append(f"{group}: invalid status")
            details(item, group, item.get("status") != "missing")
            if group == "citations":
                if type(item.get("important")) is not bool or type(item.get("cited")) is not bool:
                    errors.append("citations: important and cited booleans required")
                elif (not item["cited"]) != (item.get("status") == "missing"):
                    errors.append("citations: missing status must correspond to cited=false")
                if not item.get("claim"):
                    errors.append("citations: claim required")
    if review.get("audits", {}).get("critical_errors") is not True:
        errors.append("critical_errors: audit not confirmed")
    for item in review.get("critical_errors", []):
        details(item, "critical_errors")
        if not item.get("type"):
            errors.append("critical_errors: type required")
    return errors


def ratio(n, d):
    return n / d if d else None


def score(review):
    facts = review["facts"]
    citations = review["citations"]
    cited = [c for c in citations if c["cited"]]
    important = [c for c in citations if c["important"]]
    unknowns = review["unanswerable_targets"]
    return {
        "requirements": ratio(sum(r["score"] for r in review["requirements"]), 2 * len(review["requirements"])),
        "key_fact_coverage": ratio(sum(f["status"] == "correct" for f in facts), len(facts)),
        "answered_key_fact_accuracy": ratio(sum(f["status"] == "correct" for f in facts), sum(f["status"] != "missing" for f in facts)),
        "citation_support": ratio(sum(c["status"] == "supports" for c in cited), len(cited)),
        "citation_coverage": ratio(sum(c["status"] == "supports" for c in important), len(important)),
        "bounded_missing_accuracy": ratio(sum(u["status"] == "correct" for u in unknowns), len(unknowns)),
        "extra_unsupported_claims": sum(c["status"] == "unsupported" for c in review["extra_claims"]),
        "unverifiable_facts": sum(f["status"] == "unverifiable" for f in facts),
        "chart_accuracy": ratio(sum(c["status"] == "correct" for c in review["charts"]), len(review["charts"])),
        "critical_errors": len(review["critical_errors"]),
        "critical_error_gate_passed": not review["critical_errors"],
    }


def summarize(root):
    root = Path(root)
    dataset, manifest = read(root / "dataset.json"), read(root / "manifest.json")
    if digest(root / "dataset.json") != manifest["dataset_sha256"]:
        raise ValueError("dataset snapshot changed")
    rows = []
    for case in dataset["cases"]:
        if case["id"] not in manifest["cases"]:
            continue
        directory = root / case["id"]
        result = read(directory / "result.json") if (directory / "result.json").exists() else {"status": "not_run", "reason": "No result recorded"}
        row = {"case_id": case["id"], "execution": result["status"], "elapsed_seconds": result.get("elapsed_seconds"),
               "execution_reason": result.get("reason"), "review_errors": [], "metrics": None}
        review_path = directory / "review.json"
        if review_path.exists() and result["status"] == "completed":
            review = read(review_path)
            report_hash = digest(directory / "report.md")
            row["review_errors"] = validate_review(case, review, report_hash)
            if result.get("report_sha256") != report_hash:
                row["review_errors"].append("saved report differs from execution output")
            if not row["review_errors"]:
                row["metrics"] = score(review)
        elif result["status"] == "completed":
            row["review_errors"] = ["review missing"]
        rows.append(row)
    counts = {s: sum(r["execution"] == s for r in rows) for s in ("completed", "failed", "timed_out", "not_run")}
    reviewed = [r for r in rows if r["metrics"] is not None]
    rate_keys = ("requirements", "key_fact_coverage", "answered_key_fact_accuracy", "citation_support", "citation_coverage", "bounded_missing_accuracy", "chart_accuracy")
    macro = {}
    for key in rate_keys:
        values = [r["metrics"][key] for r in reviewed if r["metrics"][key] is not None]
        macro[key] = {"value": ratio(sum(values), len(values)), "reviewed_cases": len(values)}
    summary = {
        "dataset_id": dataset["dataset_id"], "dataset_human_review_status": dataset.get("human_review_status"),
        "counts": counts, "selected_cases": len(rows), "reviewed_cases": len(reviewed),
        "completion_rate_all_selected": ratio(counts["completed"], len(rows)),
        "completion_rate_attempted": ratio(counts["completed"], len(rows) - counts["not_run"]),
        "macro_on_reviewed_only": macro, "cases": rows,
    }
    write(root / "summary.json", summary)
    lines = ["# 评测结果", "", f"数据集：{dataset['dataset_id']}；参考标注复核状态：{dataset.get('human_review_status', 'unknown')}",
             f"选中 {len(rows)} 题，完成 {counts['completed']}，失败 {counts['failed']}，超时 {counts['timed_out']}，未执行 {counts['not_run']}；完成外部评分 {len(reviewed)} 题。", "",
             "未评分不记零分；内容指标仅代表已完成外部评分的题目，不代表全部任务。内部审核分不参与外部评分。", "",
             "| 题目 | 执行状态 | 外部评分 | 要求覆盖 | 关键事实覆盖 | 严重错误 |", "|---|---|---|---|---|---|"]
    for row in rows:
        m = row["metrics"]
        percent = lambda value: "N/A" if value is None else f"{value:.1%}"
        lines.append(f"| {row['case_id']} | {row['execution']} | {'complete' if m else 'pending / unavailable'} | {percent(m['requirements']) if m else '—'} | {percent(m['key_fact_coverage']) if m else '—'} | {m['critical_errors'] if m else '—'} |")
    lines.extend(["", "外部评分宏平均（仅含有效已评分题目）：", ""])
    for key, aggregate in macro.items():
        lines.append(f"- {key}: {percent(aggregate['value'])}；有效题数 {aggregate['reviewed_cases']}")
    lines.extend(["", "逐项分数、有效分母及评分校验错误见 summary.json。没有预设综合质量合格阈值；严重错误为零也不自动代表合格。",
                  "", "实时检索结果会变化。小样本、共享来源和待复核标注不支持行业总体质量结论。"])
    (root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Summary: {root / 'summary.md'}")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("--dataset", default=str(HERE / "pilot_dataset.json"))
    execute = sub.add_parser("run")
    execute.add_argument("--dataset", default=str(HERE / "pilot_dataset.json"))
    execute.add_argument("--output", required=True)
    execute.add_argument("--endpoint", default="http://127.0.0.1:8000/research/stream")
    execute.add_argument("--cases", help="Comma-separated case IDs")
    execute.add_argument("--timeout", type=float, default=1800)
    execute.add_argument("--configuration-note", default="Effective server configuration not verified")
    summary = sub.add_parser("summarize")
    summary.add_argument("directory")
    review = sub.add_parser("review-check")
    review.add_argument("directory")
    internal = sub.add_parser("_worker", help=argparse.SUPPRESS)
    internal.add_argument("--directory", required=True)
    internal.add_argument("--endpoint", required=True)
    internal.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    if args.command == "validate":
        validate_dataset(read(args.dataset))
        print("Dataset valid (structure only; facts still require human verification).")
    elif args.command == "run":
        run(args)
    elif args.command == "_worker":
        worker(args)
    else:
        result = summarize(args.directory)
        if args.command == "review-check":
            invalid = [r for r in result["cases"] if r["execution"] == "completed" and r["metrics"] is None]
            for row in invalid:
                print(row["case_id"], "; ".join(row["review_errors"]))
            if invalid:
                return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        sys.exit(2)
