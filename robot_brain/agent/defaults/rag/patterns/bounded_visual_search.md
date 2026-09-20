# Bounded visual search
Use when a target must be visually grounded and is not yet confirmed visible.

Recommended semantics:
1. Run VisualizeObject once to select the query and start continuous camera publication.
2. Under a finite Timeout, use ReactiveFallback with IsObjectFound first and Patrol second.
3. While IsObjectFound fails, Patrol remains RUNNING and the condition is checked again from child 1 each tick.
4. When IsObjectFound succeeds, ReactiveFallback halts Patrol and the mission continues.
5. If Timeout expires or Patrol fails, return FAILURE so the mission Sequence stops before downstream actions.

Patrol is an opaque BT Engine activity; the Agent neither designs nor assumes its internal route. Do not add RotateInPlace or an outer retry loop to this search policy.
