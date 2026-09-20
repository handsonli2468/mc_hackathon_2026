from __future__ import annotations
from typing import Any
from defusedxml import ElementTree as SafeET


def bt_metrics(xml: str | None, validation: dict[str, Any]) -> dict[str, Any]:
    out = {"valid": bool(validation.get("valid")), "errors": len(validation.get("errors", [])), "warnings": len(validation.get("warnings", [])), "node_count": 0, "condition_count": 0, "retry_count": 0, "fallback_count": 0}
    if not xml: return out
    try: root = SafeET.fromstring(xml)
    except Exception: return out
    nodes = [x for x in root.iter() if x.tag not in {"root", "BehaviorTree"}]
    out["node_count"] = len(nodes)
    out["retry_count"] = sum(1 for x in nodes if x.tag == "RetryUntilSuccessful")
    out["fallback_count"] = sum(1 for x in nodes if x.tag in {"Fallback", "ReactiveFallback"})
    out["condition_count"] = sum(1 for x in nodes if x.tag.startswith("Is") or x.tag.startswith("Has") or x.tag.startswith("Can"))
    return out
