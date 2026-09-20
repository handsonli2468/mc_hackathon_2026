from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .schemas import RequiredUserInformation


def _strip_terminal_punctuation(text: str) -> str:
    return re.sub(r"[.!?。！？]+$", "", str(text).strip())


def _looks_like_recipient_request(text: str) -> bool:
    t = str(text).lower()
    patterns = [
        r"\bbring\b.*\bto me\b",
        r"\bbring\b.*\bme\b",
        r"\bdeliver\b.*\bto me\b",
        r"\bgive\b.*\bto me\b",
        r"\breturn\b.*\bto me\b",
        r"拿.*給我",
        r"帶.*給我",
        r"送.*給我",
        r"拿.*過來",
        r"帶.*過來",
    ]
    return any(re.search(p, t, flags=re.I) for p in patterns)


def _english_visual_query(text: str) -> str | None:
    raw = _strip_terminal_punctuation(text)
    m = re.search(
        r"\b(?:i\s*am|i'm)\s+(?:standing\s+)?(beside|next\s+to|near|by|at|in\s+front\s+of|behind)\s+(.+)$",
        raw,
        flags=re.I,
    )
    if not m:
        return None
    relation = re.sub(r"\s+", " ", m.group(1).lower()).strip()
    landmark = m.group(2).strip()
    relation_map = {
        "next to": "beside",
        "by": "beside",
    }
    relation = relation_map.get(relation, relation)
    if relation in {"beside", "near", "in front of", "behind"}:
        return f"person standing {relation} {landmark}"
    return f"person {relation} {landmark}"


def _chinese_visual_query(text: str) -> str | None:
    raw = _strip_terminal_punctuation(text)
    patterns = [
        (r"我(?:現在)?在(.+?)(旁邊|旁|附近)$", lambda lm, rel: f"站在{lm}{'附近' if rel == '附近' else '旁邊'}的人"),
        (r"我(?:現在)?在(.+?)(前面|後面)$", lambda lm, rel: f"站在{lm}{rel}的人"),
        (r"我(?:現在)?在(.+)$", lambda lm, rel: f"在{lm}的人"),
    ]
    for pat, fmt in patterns:
        m = re.search(pat, raw)
        if m:
            landmark = m.group(1).strip()
            relation = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            return fmt(landmark, relation)
    return None


def infer_recipient_visual_query(original_request: str, clarification: str) -> str | None:
    if not _looks_like_recipient_request(original_request):
        return None
    return _english_visual_query(clarification) or _chinese_visual_query(clarification)


def build_pending_grounding_context(
    pending: dict[str, Any] | None,
    clarification: str,
    world_state: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach deterministic slot-filling context for a pending multi-turn mission.

    A relational reply such as "I am beside the table" is represented as a visual
    recipient query instead of being promoted into the unsupported assumption that
    the table is a known map location.
    """
    world = deepcopy(world_state or {})
    if not pending:
        return world, {}
    original = str(pending.get("original_request") or "").strip()
    query = infer_recipient_visual_query(original, clarification)
    context: dict[str, Any] = {
        "pending_mission": True,
        "original_request": original,
        "clarification": clarification,
        "resolved_fields": [],
    }
    if query:
        context.update({
            "recipient": "user",
            "recipient_grounding_mode": "VISUAL_QUERY",
            "recipient_visual_query": query,
        })
        context["resolved_fields"].append("recipient_grounding")
    world["conversation_grounding"] = context
    return world, context


def is_pending_recipient_context(world_state: dict[str, Any] | None) -> bool:
    ctx = (world_state or {}).get("conversation_grounding") or {}
    return bool(ctx.get("recipient_visual_query"))


def normalize_requirement_grounding_assumptions(requirements: Any, world_state: dict[str, Any] | None, user_message: str | None = None) -> dict[str, Any]:
    """Remove unsupported 'known map location' assumptions from requirements.

    A location is allowed to be treated as known only when world_state explicitly
    supplies it in `known_locations`. The model may still mention relational landmarks
    as visual clues; those are not navigation coordinates.
    """
    req = requirements.model_copy(deep=True)
    known_raw = (world_state or {}).get("known_locations", [])
    if isinstance(known_raw, dict):
        known = {_strip_terminal_punctuation(str(k)).lower() for k, v in known_raw.items() if v is not False}
    elif isinstance(known_raw, list):
        known = {_strip_terminal_punctuation(str(x)).lower() for x in known_raw}
    else:
        known = set()
    kept: list[str] = []
    removed: list[str] = []
    for assumption in req.assumptions:
        text = str(assumption)
        low = text.lower()
        looks_known = ("known location" in low or "known map" in low or "known navigation" in low)
        if looks_known and not any(k and k in low for k in known):
            removed.append(text)
            continue
        kept.append(text)
    req.assumptions = kept

    clarification_changes: list[str] = []
    original_request = str(user_message or "")
    ctx = (world_state or {}).get("conversation_grounding") or {}
    if _looks_like_recipient_request(original_request) or bool(ctx.get("pending_mission")):
        recipient_query = str(ctx.get("recipient_visual_query") or "").strip()
        kept_info = []
        for info in list(getattr(req, "required_user_information", []) or []):
            field = str(getattr(info, "field", "") or "").lower()
            question = str(getattr(info, "question", "") or "").lower()
            is_recipient = any(x in field for x in ("recipient", "user_location", "delivery_location", "destination"))
            is_object_location = (
                ("object" in field or "target" in field or "baseball" in question or "ball" in question)
                and any(x in field + " " + question for x in ("location", "where", "search", "visible"))
            )
            if recipient_query and is_recipient:
                clarification_changes.append(f"Resolved required field '{field}' from recipient visual grounding.")
                continue
            if is_object_location:
                clarification_changes.append(
                    f"Removed unnecessary object-location clarification '{field or question}'; VisualizeObject can search open-vocabulary targets."
                )
                continue
            kept_info.append(info)

        if not recipient_query:
            has_recipient = any(
                any(x in str(getattr(info, "field", "") or "").lower() for x in ("recipient", "user_location", "delivery_location", "destination"))
                for info in kept_info
            )
            if not has_recipient:
                kept_info.append(RequiredUserInformation(
                    field="recipient_grounding",
                    question="Where are you, or what visual description can the robot use to identify you?"
                ))
                clarification_changes.append("Prioritized recipient grounding for a delivery-to-user mission.")

        req.required_user_information = kept_info
        if recipient_query and not kept_info:
            try:
                req.status_hint = type(req.status_hint).READY
            except Exception:
                req.status_hint = "READY"
            req.message = "Recipient grounding received; continue planning the original mission."
        elif kept_info:
            try:
                req.status_hint = type(req.status_hint).NEED_MORE_INFO
            except Exception:
                req.status_hint = "NEED_MORE_INFO"
            recipient_missing = any(
                str(getattr(info, "field", "")) == "recipient_grounding" for info in kept_info
            )
            if recipient_missing:
                req.message = "Please describe where you are or give a visual clue the robot can use to identify you."

    changed = bool(removed or clarification_changes)
    warning_parts = []
    if removed:
        warning_parts.append("Removed unsupported known-location assumptions; landmarks from relational descriptions remain visual grounding clues.")
    if clarification_changes:
        warning_parts.append("Normalized clarification priorities for open-vocabulary visual search and recipient grounding.")
    return {
        "requirements": req,
        "changed": changed,
        "removed_assumptions": removed,
        "clarification_changes": clarification_changes,
        "warning": " ".join(warning_parts) if warning_parts else None,
    }
