import type { BtState } from '../api/types'

export const STATE_LABEL: Record<BtState, string> = {
  running: '執行中',
  success: '成功',
  failure: '失敗',
  canceled: '已中斷',
  error: '錯誤',
}

export function StatusBadge({ state }: { state: BtState }) {
  return <span className={`badge badge-${state}`}>{STATE_LABEL[state] ?? state}</span>
}
