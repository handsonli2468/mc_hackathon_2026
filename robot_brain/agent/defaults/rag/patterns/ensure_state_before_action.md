# Ensure state before action
When a desired state can already be observed, use an ensure-state pattern so the mutating action can be skipped safely.

Conceptual form:
Fallback(
  desired-condition,
  Sequence(action, desired-condition)
)

Examples include checking an already-satisfied navigation state before a navigation action and checking an already-held state before an acquisition action. Exact node IDs come from the active SkillManifest.
