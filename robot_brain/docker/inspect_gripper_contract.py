#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import yaml

DEFAULT = Path('/mlsteam/workspace/agent-runtime/config/bt_engine_registry.generated.yaml')


def main() -> int:
    ap = argparse.ArgumentParser(description='Print live formal contracts for gripper-related BT nodes after startup sync.')
    ap.add_argument('--registry', type=Path, default=DEFAULT)
    ap.add_argument('--nodes', nargs='*', default=['SetGripper'])
    ap.add_argument('--template-out', type=Path, help='Write a semantic-overlay scaffold for the live SetGripper ports.')
    args = ap.parse_args()
    data = yaml.safe_load(args.registry.read_text(encoding='utf-8')) or {}
    by_id = {str(x.get('id')): x for x in data.get('nodes', []) if isinstance(x, dict) and x.get('id')}
    missing = []
    for node_id in args.nodes:
        print(f'===== {node_id} =====')
        node = by_id.get(node_id)
        if node is None:
            print('NOT EXPORTED BY LIVE BT ENGINE')
            missing.append(node_id)
        else:
            print(yaml.safe_dump(node, sort_keys=False, allow_unicode=True).rstrip())
        print()
    if args.template_out and by_id.get('SetGripper') is not None:
        formal = by_id['SetGripper']
        ports = ((formal.get('ports') or {}).get('input') or {})
        template = {
            'nodes': {
                'SetGripper': {
                    'planner_enabled': True,
                    'description': 'Set jaw closure from 0 percent fully open to 100 percent fully closed.',
                    'provides': ['set_gripper_position', 'control_gripper', 'acquire_object', 'grasp_object', 'open_gripper', 'release_object'],
                    'capability_rules': {
                        'acquire_object': {'port': 'position', 'min_exclusive': 0},
                        'grasp_object': {'port': 'position', 'min_exclusive': 0},
                        'open_gripper': {'port': 'position', 'equals': 0},
                        'release_object': {'port': 'position', 'equals': 0},
                    },
                    'preconditions': [],
                    'success_semantics': ['gripper_position_command_completed'],
                    'guarantees': ['gripper_position_command_completed'],
                    'does_not_guarantee': ['object_is_held', 'object_identity'],
                    'recommended_verification': [],
                    'verification_required': False,
                    'possible_failures': ['INVALID_POSITION', 'EXECUTION_FAILED'],
                    'engine_failure_codes': [],
                    'engine_failure_codes_known': False,
                    'resources': ['GRIPPER'],
                    'object_identity_input': False,
                    'inputs': {
                        name: ({'min': 0, 'max': 100, 'unit': 'percent_closed'} if name == 'position' else {})
                        for name in ports
                    },
                }
            }
        }
        args.template_out.parent.mkdir(parents=True, exist_ok=True)
        args.template_out.write_text(yaml.safe_dump(template, sort_keys=False, allow_unicode=True), encoding='utf-8')
        print(f'Wrote semantic scaffold: {args.template_out}')
        print()
    print('v6.4 reviewed semantics: position is integer percent closed (0 fully open, 100 fully closed).')
    print('SetGripper has no object identity input; SUCCESS is treated conservatively as command completion,')
    print('not proof that an object is held. The live engine has not supplied specific failure-code names.')
    return 0 if 'SetGripper' not in missing else 2


if __name__ == '__main__':
    raise SystemExit(main())
