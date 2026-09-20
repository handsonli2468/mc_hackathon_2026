#!/usr/bin/env python3
"""End-to-end Manta check for the current continuous camera + Patrol policy."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib import error, request
import xml.etree.ElementTree as ET


def http_json(url: str, *, payload: dict | None = None, timeout: int = 900) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            result = json.loads(body) if body else {}
            if isinstance(result, dict):
                result.setdefault("_http_status", response.status)
            return result
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(body) if body else {}
        except json.JSONDecodeError:
            result = {"raw_body": body}
        if not isinstance(result, dict):
            result = {"body": result}
        result["_http_status"] = exc.code
        return result


def parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in list(parent)}


def ancestor(node: ET.Element, tag: str, parents: dict[ET.Element, ET.Element]) -> ET.Element | None:
    current = node
    while current in parents:
        current = parents[current]
        if current.tag == tag:
            return current
    return None


def validate_search_shape(xml: str) -> list[str]:
    errors: list[str] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return [f"XML parse failed: {exc}"]
    parents = parent_map(root)
    triggers = list(root.iter("VisualizeObject"))
    if not triggers:
        return ["No VisualizeObject trigger was generated."]

    for trigger in triggers:
        sequence = parents.get(trigger)
        label = trigger.attrib.get("name", "VisualizeObject")
        if sequence is None or sequence.tag != "Sequence":
            errors.append(f"{label}: trigger is not a direct child of a Sequence.")
            continue
        siblings = list(sequence)
        index = siblings.index(trigger)
        if index + 1 >= len(siblings) or siblings[index + 1].tag != "Timeout":
            errors.append(f"{label}: next sibling must be Timeout.")
            continue
        timeout = siblings[index + 1]
        timeout_children = list(timeout)
        if len(timeout_children) != 1 or timeout_children[0].tag != "ReactiveFallback":
            errors.append(f"{label}: Timeout must contain exactly one ReactiveFallback.")
            continue
        monitor = timeout_children[0]
        branches = list(monitor)
        if len(branches) != 2:
            errors.append(f"{label}: ReactiveFallback must have exactly two branches.")
            continue
        if branches[0].tag != "IsObjectFound":
            errors.append(f"{label}: ReactiveFallback child 1 must be IsObjectFound.")
        if branches[1].tag != "Patrol":
            errors.append(f"{label}: ReactiveFallback child 2 must be Patrol.")
        if branches[0].attrib.get("object_name") != trigger.attrib.get("object_name"):
            errors.append(f"{label}: VisualizeObject and IsObjectFound object_name differ.")
        if ancestor(trigger, "Timeout", parents) is not None:
            errors.append(f"{label}: trigger must execute before, not inside, Timeout.")
        if any(node.tag in {"RotateInPlace", "RetryUntilSuccessful", "ForceFailure"} for node in sequence.iter()):
            errors.append(f"{label}: obsolete rotate/retry search node appears in the search Sequence.")
    return errors


def print_section(title: str, value) -> None:
    print(f"\n===== {title} =====")
    if isinstance(value, str):
        print(value or "-")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and inspect the current continuous-search BehaviorTree on Manta.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--message", default="Find the bottle, move near it, and close the gripper to 60 percent.")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output-dir", default="agent-runtime/evaluations/continuous-search")
    parser.add_argument("--engine-validate", action="store_true", help="Also POST the saved mission to the live BT Engine validator.")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    health = http_json(base + "/health", timeout=30)
    response = http_json(
        base + "/api/chat",
        payload={
            "pipeline_mode": "hybrid",
            "message": args.message,
            "world_state": {},
            "options": {"auto_execute": False},
        },
        timeout=args.timeout,
    )
    candidate = response.get("candidate") or {}
    xml = str(candidate.get("bt_xml") or "")
    shape_errors = validate_search_shape(xml) if xml else ["Response has no bt_xml."]

    print_section("HEALTH", {
        "ok": health.get("ok"),
        "version": health.get("version"),
        "engine_custom_nodes": (health.get("bt_engine_contract") or {}).get("engine_node_count"),
        "builtin_nodes": (health.get("bt_engine_contract") or {}).get("builtin_node_count"),
        "effective_nodes": (health.get("bt_engine_contract") or {}).get("effective_node_count"),
        "semantic_complete": (health.get("bt_engine_contract") or {}).get("semantic_complete"),
        "node_sync_ok": (health.get("bt_node_sync") or {}).get("ok"),
    })
    print_section("AGENT RESPONSE", {
        "session_id": response.get("session_id"),
        "mission_id": candidate.get("mission_id"),
        "status": candidate.get("status"),
        "message": candidate.get("message"),
    })
    print_section("PLAN HARDENING", candidate.get("plan_hardening") or {})
    print_section("COMPILER IR", candidate.get("compiler_task_plan_ir") or candidate.get("task_plan_ir") or {})
    print_section("COMPILER WARNINGS", candidate.get("compiler_warnings") or [])
    print_section("BT VALIDATION", candidate.get("bt_validation") or {})
    print_section("SEARCH SHAPE CHECK", {"valid": not shape_errors, "errors": shape_errors})
    print_section("BEHAVIOR TREE XML", xml)
    print_section("TIMING", candidate.get("timing") or {})

    engine_result = None
    mission_id = candidate.get("mission_id")
    if args.engine_validate and mission_id:
        engine_result = http_json(
            f"{base}/api/missions/{mission_id}/engine/validate",
            payload={},
            timeout=60,
        )
        print_section("LIVE ENGINE VALIDATION", engine_result)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"continuous-search-{stamp}.json"
    xml_path = output_dir / f"continuous-search-{stamp}.xml"
    json_path.write_text(json.dumps({
        "health": health,
        "response": response,
        "search_shape": {"valid": not shape_errors, "errors": shape_errors},
        "engine_validation": engine_result,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved full JSON: {json_path}")
    if xml:
        xml_path.write_text(xml, encoding="utf-8")
        print(f"Saved XML:       {xml_path}")
    else:
        print("Saved XML:       skipped (response contained no bt_xml)")

    failures = []
    if not health.get("ok"):
        failures.append("health.ok is false")
    if candidate.get("status") != "SUCCESS":
        failures.append(f"candidate.status={candidate.get('status')!r}")
    if not (candidate.get("bt_validation") or {}).get("valid"):
        failures.append("Agent bt_validation.valid is false")
    failures.extend(shape_errors)
    if engine_result is not None and (
        int(engine_result.get("_http_status", 200)) >= 400
        or engine_result.get("valid") is False
        or engine_result.get("ok") is False
    ):
        failures.append("Live engine validation rejected the XML")
    if failures:
        raise SystemExit("FAILED: " + " | ".join(failures))
    print("\nPASS: continuous search policy and deterministic validation are correct.")


if __name__ == "__main__":
    main()
