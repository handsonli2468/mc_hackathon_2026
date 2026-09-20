import type {
  BtStatus,
  CalibResult,
  CalibRunResponse,
  CalibState,
  ChatReply,
  CloudHealth,
  FeedbackResponse,
  FieldConfig,
  MissionFeedback,
  RunMission,
  RunSummary,
} from './types'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, '連不上 App Gateway')
  }
  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!res.ok) {
    throw new ApiError(res.status, errorMessage(data) ?? `HTTP ${res.status}`)
  }
  return data as T
}

function errorMessage(data: unknown): string | undefined {
  if (!data || typeof data !== 'object') return undefined
  const d = data as { error?: unknown; detail?: unknown }
  if (typeof d.error === 'string') return d.error
  if (typeof d.detail === 'string') return d.detail
  if (Array.isArray(d.detail)) {
    return d.detail.map((e: { loc?: unknown[]; msg?: string }) => `${e.loc?.slice(1).join('.')}: ${e.msg}`).join('; ')
  }
  return undefined
}

export const api = {
  cloudHealth: () => call<CloudHealth>('GET', '/api/cloud/health'),
  chat: (message: string, sessionId: string | null) =>
    call<ChatReply>('POST', '/api/chat', { message, session_id: sessionId }),
  resetSession: (sessionId: string | null) =>
    call<{ session_id: string }>('POST', '/api/sessions/reset', { session_id: sessionId }),
  cancelMission: (missionId: string) =>
    call<{ ok: boolean; was_running?: boolean }>('POST', `/api/missions/${encodeURIComponent(missionId)}/cancel`),
  missionFeedback: (missionId: string, fb: MissionFeedback) =>
    call<FeedbackResponse>('POST', `/api/missions/${encodeURIComponent(missionId)}/feedback`, fb),
  runMission: (runId: string) => call<RunMission>('GET', `/api/runs/${encodeURIComponent(runId)}/mission`),

  btStatus: (runId?: string, fullTrace = false) =>
    call<BtStatus>('GET', `/api/bt/status${runId ? `/${encodeURIComponent(runId)}` : ''}${fullTrace ? '?trace=full' : ''}`),
  btRuns: async (): Promise<RunSummary[]> => {
    const data = await call<RunSummary[] | { runs: RunSummary[] }>('GET', '/api/bt/runs')
    return Array.isArray(data) ? data : (data.runs ?? [])
  },
  btCancel: () => call<{ ok: boolean; was_running: boolean }>('POST', '/api/bt/cancel'),
  btHealth: () => call<{ ok: boolean }>('GET', '/api/bt/health'),

  getField: () => call<FieldConfig>('GET', '/api/calib/field'),
  putField: (cfg: FieldConfig) => call<FieldConfig>('PUT', '/api/calib/field', cfg),
  calibState: () => call<CalibState>('GET', '/api/calib/state'),
  calibRun: () => call<CalibRunResponse>('POST', '/api/calib/run'),
  calibResult: () => call<CalibResult>('GET', '/api/calib/result'),
  calibLatestRun: () => call<CalibRunResponse>('GET', '/api/calib/runs/latest'),
}

/** Latest run, or null when bt_engine has never run a tree (it answers 404). */
export async function latestStatus(): Promise<BtStatus | null> {
  try {
    return await api.btStatus()
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

export function noteText(n: { node: string; message: string }): string {
  return `${n.node}: ${n.message}`
}
