from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import yaml

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
ROOT = Path(os.getenv("RUNTIME_ROOT", "/mlsteam/workspace/agent-runtime"))
CASES_PATH = Path(os.getenv("REGRESSION_CASES", "agent/tests/regression_cases.yaml"))
PIPELINES = [x.strip() for x in os.getenv("REGRESSION_PIPELINES", "direct,hybrid").split(",") if x.strip()]


def xml_nodes(xml: str | None) -> list[str]:
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except Exception:
        return []
    return [e.tag for e in root.iter() if e.tag not in {"root", "BehaviorTree"}]


def property_checks(candidate: dict[str, Any], requested: list[str]) -> dict[str, bool]:
    xml = candidate.get("bt_xml")
    nodes = xml_nodes(xml)
    validation = candidate.get("bt_validation") or {}
    checks: dict[str, bool] = {}

    if "blackboard_valid" in requested:
        checks["blackboard_valid"] = bool(validation.get("valid"))

    if "observable_grasp_verification" in requested:
        if "PickObject" not in nodes:
            checks["observable_grasp_verification"] = False
        else:
            pick_idx = nodes.index("PickObject")
            checks["observable_grasp_verification"] = "IsObjectHeld" in nodes[pick_idx + 1 :]

    if "bounded_retry" in requested:
        ok = True
        if xml:
            try:
                root = ET.fromstring(xml)
                retries = [e for e in root.iter() if e.tag == "RetryUntilSuccessful"]
                if not retries:
                    ok = False
                for e in retries:
                    raw = e.attrib.get("num_attempts")
                    try:
                        n = int(raw or "")
                        ok = ok and 1 <= n <= 10
                    except ValueError:
                        ok = False
            except Exception:
                ok = False
        else:
            ok = False
        checks["bounded_retry"] = ok

    if "recovery_structure" in requested:
        metrics = candidate.get("metrics") or {}
        checks["recovery_structure"] = int(metrics.get("retry_count", 0)) > 0 or int(metrics.get("fallback_count", 0)) > 0

    return checks


def score_candidate(candidate: dict[str, Any], case: dict[str, Any], pipeline: str) -> dict[str, Any]:
    expected = case.get("expected", {})
    checks: dict[str, bool] = {}
    expected_status = expected.get("status")
    checks["status"] = candidate.get("status") == expected_status

    if expected.get("no_bt"):
        checks["no_bt"] = not bool(candidate.get("bt_xml"))

    required_nodes = expected.get("required_nodes", []) or []
    if required_nodes:
        nodes = xml_nodes(candidate.get("bt_xml"))
        for node in required_nodes:
            checks[f"node:{node}"] = node in nodes

    checks.update(property_checks(candidate, expected.get("properties", []) or []))

    if expected_status == "SUCCESS":
        checks["bt_validation"] = bool((candidate.get("bt_validation") or {}).get("valid"))
        if pipeline == "hybrid":
            checks["task_plan_ir_present"] = bool(candidate.get("task_plan_ir"))
            checks["ir_validation"] = bool((candidate.get("ir_validation") or {}).get("valid"))

    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    return {
        "pass": total > 0 and passed == total,
        "passed_checks": passed,
        "total_checks": total,
        "checks": checks,
    }


def main() -> int:
    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    results: list[dict[str, Any]] = []
    aggregate = {p: {"cases_passed": 0, "cases_total": 0, "checks_passed": 0, "checks_total": 0} for p in PIPELINES}

    with httpx.Client(timeout=1800) as client:
        for case in cases:
            payload = {
                "message": case["request"],
                "pipeline_mode": "compare" if set(PIPELINES) == {"direct", "hybrid"} else PIPELINES[0],
                "world_state": case.get("world_state") or {},
                "options": {"auto_execute": False},
            }
            started = time.time()
            r = client.post(BASE + "/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            elapsed = time.time() - started

            if data.get("pipeline_mode") == "compare":
                candidates = data.get("candidates", {})
            else:
                candidates = {PIPELINES[0]: data.get("candidate", {})}

            scored: dict[str, Any] = {}
            print(f"\n=== {case['id']} ===")
            for pipeline in PIPELINES:
                candidate = candidates.get(pipeline, {})
                score = score_candidate(candidate, case, pipeline)
                scored[pipeline] = score
                agg = aggregate[pipeline]
                agg["cases_total"] += 1
                agg["cases_passed"] += int(score["pass"])
                agg["checks_total"] += score["total_checks"]
                agg["checks_passed"] += score["passed_checks"]
                print(
                    f"{pipeline:7s} status={candidate.get('status')} "
                    f"case={'PASS' if score['pass'] else 'FAIL'} "
                    f"checks={score['passed_checks']}/{score['total_checks']} elapsed={candidate.get('elapsed_sec')}s repairs(ir/bt)={candidate.get('ir_repair_attempts',0)}/{candidate.get('bt_repair_attempts',0)} "
                    f"compiler_fallbacks={candidate.get('compiler_fallback_count',0)}"
                )
                for name, ok in score["checks"].items():
                    if not ok:
                        print(f"  FAIL {name}")
                if candidate.get("status") == "PLANNING_FAILURE":
                    print("  message:", candidate.get("message"))
                    if candidate.get("ir_validation", {}).get("errors"):
                        print("  IR errors:", candidate["ir_validation"]["errors"])
                    if candidate.get("bt_validation", {}).get("errors"):
                        print("  BT errors:", candidate["bt_validation"]["errors"])

            results.append({"case": case, "elapsed_sec": elapsed, "response": data, "score": scored})

    print("\n=== SUMMARY ===")
    for pipeline, agg in aggregate.items():
        case_rate = 100.0 * agg["cases_passed"] / max(1, agg["cases_total"])
        check_rate = 100.0 * agg["checks_passed"] / max(1, agg["checks_total"])
        print(
            f"{pipeline:7s} cases {agg['cases_passed']}/{agg['cases_total']} ({case_rate:.1f}%) | "
            f"checks {agg['checks_passed']}/{agg['checks_total']} ({check_rate:.1f}%)"
        )

    out = ROOT / "evaluations" / f"regression-v5.2-{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"aggregate": aggregate, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("saved", out)

    # Return non-zero only if every requested pipeline completely failed every case.
    # This keeps the script useful as an experiment harness while still surfacing a
    # catastrophic regression to shell/CI.
    catastrophic = all(v["cases_passed"] == 0 for v in aggregate.values())
    return 2 if catastrophic else 0


if __name__ == "__main__":
    raise SystemExit(main())
