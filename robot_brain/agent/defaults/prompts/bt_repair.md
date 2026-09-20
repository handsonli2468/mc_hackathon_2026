Repair the BehaviorTree XML using the supplied deterministic validation errors.
Return exactly one complete <root>...</root> XML document and nothing else.
Do not invent leaf nodes, do not remove required task steps, and do not create unbounded retry/recovery loops.
For semantic errors, change only the control-flow structure needed to make the planned action, verification, retry, recovery, and cleanup semantics correct.
