// Shapes returned by the gateway. BT ones mirror bt_engine (mc_main_nav docs/llm_interface.md).

export type BtState = 'running' | 'success' | 'failure' | 'canceled' | 'error'

export interface TraceEvent {
  t: number
  node: string
  type: string
  from: string
  to: string
}

export interface BtNote {
  node: string
  message: string
}

export interface BtStatus {
  run_id: string
  state: BtState
  running_leaves: string[]
  elapsed_s: number
  notes: BtNote[]
  last_leaf_failure: TraceEvent | null
  trace: TraceEvent[]
  trace_truncated?: boolean
  started_at: number
  finished_at?: number
  error?: string
}

export interface RunSummary {
  run_id: string
  state: BtState
  started_at: number
}

export type GripForce = 'too_weak' | 'ok' | 'too_strong'

export interface PlanStep {
  action: string
  arguments: Record<string, unknown>
  objective: string
}

/** Manta's auto_execution after a tree was generated. */
export interface ChatExecution {
  status: 'STARTED' | 'FAILED' | 'SKIPPED' | null
  run_id: string | null
  engine_state: string | null
  preempted_previous: boolean
  reason: string | null
  error: string | null
}

/** Manta /api/chat, trimmed by the gateway. A generated tree is started by Manta itself. */
export interface ChatReply {
  session_id: string | null
  status: string | null // SUCCESS | NEED_MORE_INFO | PLANNING_FAILURE | UNSUPPORTED | UNSAFE | ...
  message: string
  questions: string[]
  missing_capabilities: string[]
  mission_id: string | null
  bt_generated: boolean
  generation_message: string
  execution: ChatExecution
  goal: string
  steps: PlanStep[]
  gripper_position: number | null
  bt_xml: string | null
}

export interface RunMission {
  mission_id: string
  prompt: string
  t: number
}

export interface MissionFeedback {
  rating: number
  comment?: string
  grip_force?: GripForce
}

export interface FeedbackResponse {
  ok: boolean
  sent: { rating: number; comment: string; parameters?: { set_gripper_position: number } }
  base_gripper_position: number | null
}

export interface CloudHealth {
  ok: boolean
  mode: 'mock' | 'http'
  version?: string
}

export interface FieldConfig {
  length: number
  depth: number
  count: number
  disabled_segments: string[]
}

export interface SegmentQuality {
  accepted: number
  samples: number
  rms_px: number
  mean_px: number
}

export interface CalibResult {
  calibrated_at: string
  table: { length: number; depth: number; count: number }
  cam_tf: { x: number; y: number; z: number; roll: number; pitch: number; yaw: number }
  table_dx?: number[]
  init?: string
  passed: boolean
  problems: string[]
  quality: {
    accept_ratio: number
    rms_px: number
    segments: Record<string, SegmentQuality>
    depth_plane?: Record<string, number>
  }
}

export interface CalibRunResponse {
  success: boolean
  message: string
  run: string | null
  result: CalibResult | null
}

export interface CalibState {
  mode: 'ros' | 'mock'
  error: string | null
  connected: boolean
  running_since: number | null
  latest_run: string | null
  streams: Record<'live' | 'calib' | 'camera', number | null>
  frame_counts?: Record<'live' | 'calib' | 'camera', number>
  sources?: Record<'live' | 'calib' | 'camera', string | null>
}
