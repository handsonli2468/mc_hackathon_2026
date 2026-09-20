import type { TraceEvent } from '../api/types'

const STATUS_CLASS: Record<string, string> = {
  RUNNING: 'ev-running',
  SUCCESS: 'ev-success',
  FAILURE: 'ev-failure',
  IDLE: 'ev-idle',
}

/** Newest first: status changes of each tree node. */
export function RunTimeline({ trace, truncated }: { trace: TraceEvent[]; truncated?: boolean }) {
  if (!trace.length) return <p className="muted">還沒有狀態變化</p>
  return (
    <ol className="timeline">
      {[...trace].reverse().map((ev, i) => (
        <li key={`${ev.t}-${ev.node}-${i}`} className={STATUS_CLASS[ev.to] ?? ''}>
          <span className="tl-time">{ev.t.toFixed(1)}s</span>
          <span className="tl-node">{ev.node}</span>
          <span className="tl-change">
            {ev.from} → <b>{ev.to}</b>
          </span>
        </li>
      ))}
      {truncated && <li className="muted">（較早的紀錄已省略）</li>}
    </ol>
  )
}
