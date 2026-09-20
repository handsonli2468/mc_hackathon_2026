from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "agent-runtime"

# Deliberately varies task shape, language and RAG relevance. The goal is to test
# semantic flexibility rather than benchmark the same prompt five times.
DEFAULT_DIVERSE_MESSAGES = [
    "find the blue box and grab it",
    "找到 bottle，靠近它，但先不要拿起來。",
    "Please locate the presentation remote and move near it.",
    "Search for the marker and grasp it securely.",
    "在附近找到 cup，接近它並拿起來。",
]


def post_json(url: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fmt(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.3f}"


def _stats(values: list[float]) -> dict:
    if not values:
        return {}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
    }


def summarize(results: list[dict]) -> dict:
    totals: list[float] = []
    statuses: dict[str, int] = defaultdict(int)
    model_elapsed: dict[str, list[float]] = defaultdict(list)
    model_calls: dict[str, list[int]] = defaultdict(list)
    stage_elapsed: dict[str, list[float]] = defaultdict(list)
    per_input: list[dict] = []

    for item in results:
        candidate = item.get("candidate") or {}
        status = str(candidate.get("status") or "UNKNOWN")
        statuses[status] += 1
        timing = candidate.get("timing") or {}
        total = timing.get("total_elapsed_sec")
        if isinstance(total, (int, float)):
            totals.append(float(total))
        for role, bucket in (timing.get("model_summary") or {}).items():
            if isinstance(bucket, dict):
                model_elapsed[role].append(float(bucket.get("elapsed_sec", 0.0) or 0.0))
                model_calls[role].append(int(bucket.get("calls", 0) or 0))
        for name, bucket in (timing.get("stage_summary") or {}).items():
            if isinstance(bucket, dict):
                stage_elapsed[name].append(float(bucket.get("elapsed_sec", 0.0) or 0.0))
        per_input.append(
            {
                "run": item.get("profile_run_index"),
                "message": item.get("profile_input_message"),
                "status": status,
                "total_elapsed_sec": total,
                "compact_planner_used": bool(candidate.get("compact_planner_used", False)),
                "planner_escalated": bool(candidate.get("planner_escalated", False)),
                "critic_attempted": bool(candidate.get("critic_attempted", False)),
                "compiler_fallback_count": int(candidate.get("compiler_fallback_count", 0) or 0),
                "rag_counts": (candidate.get("rag_context") or {}).get("counts", {}),
                "rag_missing_capabilities": (candidate.get("rag_context") or {}).get("missing_capabilities", []),
            }
        )

    return {
        "runs": len(results),
        "statuses": dict(statuses),
        "total_elapsed_sec": _stats(totals),
        "model_elapsed_sec": {role: _stats(values) for role, values in model_elapsed.items()},
        "model_calls": {
            role: {
                "min": min(values),
                "max": max(values),
                "mean": statistics.fmean(values),
                "median": statistics.median(values),
            }
            for role, values in model_calls.items()
            if values
        },
        "stage_elapsed_sec": {name: _stats(values) for name, values in stage_elapsed.items()},
        "per_input": per_input,
    }


def print_run(index: int, data: dict) -> None:
    candidate = data.get("candidate") or {}
    timing = candidate.get("timing") or {}
    print(f"\n===== RUN {index} =====")
    print(f"input:  {data.get('profile_input_message', '-')}")
    print(f"status: {candidate.get('status')}")
    print(f"mission_id: {candidate.get('mission_id')}")
    print(f"agent_message: {candidate.get('message', '')}")
    generation = candidate.get("bt_generation") or {}
    auto_execution = candidate.get("auto_execution") or {}
    print(
        f"bt_generated: {generation.get('succeeded', False)}  "
        f"auto_execution: {auto_execution.get('status', '-')} "
        f"({auto_execution.get('reason') or auto_execution.get('run_id') or '-'})"
    )
    print(f"total:  {fmt(timing.get('total_elapsed_sec'))} sec")
    print(f"fallback_count: {candidate.get('compiler_fallback_count', 0)}")
    print(f"critic_attempted: {candidate.get('critic_attempted', False)}")
    print(
        f"compact_planner_used: {candidate.get('compact_planner_used', False)}  "
        f"planner_escalated: {candidate.get('planner_escalated', False)}"
    )
    rag = candidate.get("rag_context") or {}
    print(
        f"rag: strategy={rag.get('strategy', '-')} counts={rag.get('counts', {})} "
        f"missing={rag.get('missing_capabilities', [])}"
    )
    if rag.get("allowed_skill_ids"):
        print("  rag skills: " + ", ".join(str(x) for x in rag.get("allowed_skill_ids", [])))
    if rag.get("patterns"):
        print("  rag patterns: " + ", ".join(str(x.get("id")) for x in rag.get("patterns", [])))
    if rag.get("scene"):
        print("  rag scene: " + ", ".join(str(x.get("id")) for x in rag.get("scene", [])))
    if rag.get("experiences"):
        print("  rag experiences: " + ", ".join(str(x.get("id")) for x in rag.get("experiences", [])))
    usage = candidate.get("rag_utilization") or {}
    scene_locations = ((usage.get("scene") or {}).get("locations") or {})
    if scene_locations:
        print("  rag location usage:")
        for loc_id, info in scene_locations.items():
            print(
                f"    {loc_id}: actionable={info.get('actionable')} referenced={info.get('referenced_in_plan')} "
                f"reason={info.get('reason')}"
            )
    compact_norm = candidate.get("compact_plan_normalization") or {}
    print(
        f"compact_plan_normalization: changed={compact_norm.get('changed', False)} "
        f"changes={len(compact_norm.get('changes', []) or [])}"
    )
    if compact_norm.get("changes"):
        print("  compact: " + " | ".join(str(x) for x in compact_norm.get("changes", [])))
    if candidate.get("compact_fallback_reason"):
        print(f"compact_fallback_reason: {candidate.get('compact_fallback_reason')}")
    grounding = candidate.get("grounding_policy") or {}
    print(f"grounding_policy: changed={grounding.get('changed', False)} changes={len(grounding.get('changes', []) or [])}")
    if grounding.get("changes"):
        print("  grounding: " + " | ".join(str(x) for x in grounding.get("changes", [])))
    contract_norm = candidate.get("skill_contract_normalization") or {}
    print(
        f"skill_contract_normalization: changed={contract_norm.get('changed', False)} "
        f"changes={len(contract_norm.get('changes', []) or [])}"
    )
    if contract_norm.get("changes"):
        print("  contract: " + " | ".join(str(x) for x in contract_norm.get("changes", [])))
    hardening = candidate.get("plan_hardening") or {}
    print(f"plan_hardening: changed={hardening.get('changed', False)} changes={len(hardening.get('changes', []) or [])}")
    if hardening.get("changes"):
        print("  hardening: " + " | ".join(str(x) for x in hardening.get("changes", [])))
    before = candidate.get("quality_gate_before_critic") or {}
    after = candidate.get("quality_gate_after_critic") or {}
    final_gate = candidate.get("quality_gate") or {}
    print(f"quality_gate_before_critic: pass={before.get('pass')} needs_critic={before.get('needs_critic')}")
    if before.get("critical_issues"):
        print("  critic reasons: " + " | ".join(str(x) for x in before.get("critical_issues", [])))
    print(f"quality_gate_after_critic:  pass={after.get('pass') if after else '-'}")
    print(f"quality_gate_final:         pass={final_gate.get('pass')}")
    print(f"ir_repairs: {candidate.get('ir_repair_attempts', 0)}  bt_repairs: {candidate.get('bt_repair_attempts', 0)}")
    validation = candidate.get("bt_validation") or {}
    print(f"bt_validation: valid={validation.get('valid')} errors={len(validation.get('errors', []) or [])}")
    if validation.get("errors"):
        print("  validation errors: " + " | ".join(str(x) for x in validation.get("errors", [])))
    if candidate.get("compiler_warnings"):
        print("  compiler warnings: " + " | ".join(str(x) for x in candidate.get("compiler_warnings", [])))
    print("model summary:")
    for role, bucket in (timing.get("model_summary") or {}).items():
        print(
            f"  {role:9s} calls={bucket.get('calls', 0):2d} "
            f"elapsed={fmt(bucket.get('elapsed_sec'))}s "
            f"prompt_tokens={bucket.get('prompt_tokens', 0)} "
            f"completion_tokens={bucket.get('completion_tokens', 0)} "
            f"reasoning_tokens={bucket.get('reasoning_tokens', 0)} "
            f"budgeted_calls={bucket.get('budgeted_calls', 0)}"
        )
    print("slowest stages:")
    stages = sorted(
        (timing.get("stage_summary") or {}).items(),
        key=lambda kv: float((kv[1] or {}).get("elapsed_sec", 0.0)),
        reverse=True,
    )
    for name, bucket in stages[:10]:
        print(f"  {name:32s} {fmt(bucket.get('elapsed_sec'))}s  calls={bucket.get('calls', 0)}")


def _load_messages_file(path: str) -> list[str]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        value = json.loads(text)
        if not isinstance(value, list):
            raise SystemExit("--messages-file JSON must contain a list of strings")
        messages = [str(x).strip() for x in value if str(x).strip()]
    else:
        messages = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not messages:
        raise SystemExit("--messages-file did not contain any non-empty messages")
    return messages


def choose_messages(runs: int, message: str | None, messages_file: str | None) -> tuple[list[str], str]:
    if message:
        return [message] * runs, "single_explicit"
    pool = _load_messages_file(messages_file) if messages_file else DEFAULT_DIVERSE_MESSAGES
    return [pool[i % len(pool)] for i in range(runs)], "diverse"


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile Robot Brain end-to-end timing with diverse semantic inputs by default.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--pipeline", choices=["hybrid", "direct"], default="hybrid")
    parser.add_argument(
        "--message",
        default=None,
        help="Explicitly repeat one message for every run. If omitted, the profiler rotates through a diverse built-in suite.",
    )
    parser.add_argument(
        "--messages-file",
        default=None,
        help="Optional newline-delimited or JSON-list message suite. Ignored when --message is supplied.",
    )
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--world-state", default="{}", help="JSON object")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")
    world_state = json.loads(args.world_state)
    if not isinstance(world_state, dict):
        raise SystemExit("--world-state must decode to a JSON object")

    messages, message_mode = choose_messages(args.runs, args.message, args.messages_file)
    results: list[dict] = []
    for idx, message in enumerate(messages, 1):
        started = time.perf_counter()
        data = post_json(
            args.base_url.rstrip("/") + "/api/chat",
            {
                "pipeline_mode": args.pipeline,
                "message": message,
                "world_state": world_state,
                "options": {"auto_execute": False},
            },
            timeout=args.timeout,
        )
        wall = time.perf_counter() - started
        data["client_wall_elapsed_sec"] = wall
        data["profile_run_index"] = idx
        data["profile_input_message"] = message
        results.append(data)
        print_run(idx, data)

    summary = summarize(results)
    print("\n===== PROFILE SUMMARY =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    out = Path(args.output) if args.output else (
        RUNTIME / "evaluations" / "timing" / f"profile-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "configuration": {
                    "runs": args.runs,
                    "pipeline": args.pipeline,
                    "message_mode": message_mode,
                    "message": args.message,
                    "messages": messages,
                    "messages_file": args.messages_file,
                    "world_state": world_state,
                    "note": "Current profile. Multi-run profiling uses varied semantic inputs by default. Pass --message only when intentionally measuring repeated-prompt variance. RAG remains local SQLite FTS5 and adds no model call. Use docker/test_continuous_search.py for complete IR/XML/validation output.",
                },
                "summary": summary,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nSaved full timing profile: {out}")


if __name__ == "__main__":
    main()
