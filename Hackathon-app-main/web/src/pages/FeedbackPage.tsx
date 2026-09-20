import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ApiError, api, noteText } from '../api/client'
import type { BtStatus, FeedbackResponse, GripForce, RunMission } from '../api/types'
import { RunTimeline } from '../components/RunTimeline'
import { StatusBadge } from '../components/StatusBadge'
import { promptFor } from '../lib/store'
import { usePolling } from '../lib/usePolling'
import type { TaskPageState } from './TaskPage'

const GRIP_OPTIONS: { value: GripForce; label: string }[] = [
  { value: 'too_weak', label: '太小（沒夾住／滑掉）' },
  { value: 'ok', label: '剛好' },
  { value: 'too_strong', label: '太大（夾壞／變形）' },
]

export function FeedbackPage() {
  const { runId = '' } = useParams()
  const navigate = useNavigate()
  const [status, setStatus] = useState<BtStatus | null>(null)
  const [loadError, setLoadError] = useState('')
  const [mission, setMission] = useState<RunMission | null | undefined>(undefined) // undefined = loading

  usePolling(
    async () => {
      try {
        setStatus(await api.btStatus(runId, true))
        setLoadError('')
      } catch (e) {
        setLoadError(e instanceof Error ? e.message : String(e))
      }
    },
    1000,
    !status || status.state === 'running',
  )

  useEffect(() => {
    api.runMission(runId).then(setMission, () => setMission(null))
  }, [runId])

  const prompt = mission?.prompt || promptFor(runId)
  const failed = status && status.state !== 'success' && status.state !== 'running'

  return (
    <div className="page">
      <Link to="/" className="back">
        ← 回任務
      </Link>
      <section className="card">
        <div className="row between">
          <h2>任務結果</h2>
          {status && <StatusBadge state={status.state} />}
        </div>
        {loadError && <p className="error">{loadError}</p>}
        {!status && !loadError && <p className="muted">載入中…</p>}
        {status && (
          <>
            <p className="muted">
              <code>{status.run_id}</code>・{status.elapsed_s?.toFixed(1)} 秒{prompt && <>・「{prompt}」</>}
            </p>
            {status.state === 'running' && <p>任務仍在執行中，結束後這裡會自動更新。</p>}
            {status.state === 'success' && <p className="ok-text">任務完成！請留下回饋，幫助機器人下次做得更好。</p>}
            {failed && <FailureDetails status={status} />}
            <details>
              <summary>完整執行紀錄</summary>
              <RunTimeline trace={status.trace ?? []} truncated={status.trace_truncated} />
            </details>
          </>
        )}
      </section>

      {failed && (
        <RetryForm
          status={status}
          prompt={prompt}
          onSend={(text) => navigate('/', { state: { send: text } satisfies TaskPageState })}
        />
      )}
      {status && status.state !== 'running' && mission !== undefined && <FeedbackForm mission={mission} />}
    </div>
  )
}

function FailureDetails({ status }: { status: BtStatus }) {
  return (
    <div className="failure">
      <h3>{status.state === 'canceled' ? '任務被中斷' : '失敗原因'}</h3>
      {status.error && <pre className="error-box">{status.error}</pre>}
      {status.notes.length > 0 ? (
        <ul>
          {status.notes.map((n, i) => (
            <li key={i}>
              <b>{n.node}</b>：{n.message}
            </li>
          ))}
        </ul>
      ) : (
        !status.error && <p className="muted">{status.state === 'canceled' ? '由使用者或新的任務中斷。' : '沒有回報具體原因。'}</p>
      )}
      {status.last_leaf_failure && (
        <p className="muted">
          最後失敗的步驟：<code>{status.last_leaf_failure.node}</code>（{status.last_leaf_failure.type}）
        </p>
      )}
    </div>
  )
}

function failureSummary(status: BtStatus): string {
  const reasons = status.notes.map(noteText).concat(status.error ? [status.error] : [])
  if (status.state === 'canceled') return '上一次執行被中斷了。'
  return reasons.length ? `上一次執行失敗，原因：${reasons.join('；')}` : '上一次執行失敗了。'
}

function RetryForm({ status, prompt, onSend }: { status: BtStatus; prompt: string; onSend: (text: string) => void }) {
  const [text, setText] = useState(prompt)

  function submit(e: React.FormEvent) {
    e.preventDefault()
    const t = text.trim()
    if (t) onSend(`${t}\n\n（${failureSummary(status)}）`)
  }

  return (
    <section className="card">
      <h2>修改指令重來</h2>
      <p className="muted">會在原本的對話中送出，並附上失敗原因，讓雲端重新規劃。規劃成功後機器人會直接開始執行。</p>
      <form onSubmit={submit} className="stack">
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={3000} placeholder="輸入新的指令" />
        <button type="submit" className="primary" disabled={!text.trim()}>
          重新規劃
        </button>
      </form>
    </section>
  )
}

function FeedbackForm({ mission }: { mission: RunMission | null }) {
  const [grip, setGrip] = useState<GripForce | undefined>()
  const [rating, setRating] = useState<number | undefined>()
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState<FeedbackResponse | null>(null)
  const [error, setError] = useState('')

  if (!mission) {
    return (
      <section className="card">
        <h2>回饋</h2>
        <p className="muted">這個任務不是從 App 發布的，找不到對應的雲端任務，所以無法送出回饋。</p>
      </section>
    )
  }
  const missionId = mission.mission_id

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!rating) return
    setBusy(true)
    setError('')
    try {
      setSent(await api.missionFeedback(missionId, { rating, grip_force: grip, comment: comment.trim() }))
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? '雲端還不能收這個任務的回饋（任務可能尚未結束），請稍後再試。'
          : e instanceof Error
            ? e.message
            : String(e),
      )
    } finally {
      setBusy(false)
    }
  }

  if (sent) {
    const p = sent.sent.parameters?.set_gripper_position
    return (
      <section className="card">
        <h2>回饋</h2>
        <p className="ok-text">謝謝！回饋已送出。</p>
        {p !== undefined && sent.base_gripper_position !== null && (
          <p className="muted">
            夾爪建議值：{sent.base_gripper_position} → <b>{p}</b>（0 全開、100 全閉），下次類似任務會參考。
          </p>
        )}
        {grip && p === undefined && <p className="muted">這次的行為樹沒有夾爪動作，所以沒有送出夾爪建議值。</p>}
      </section>
    )
  }

  return (
    <section className="card">
      <h2>回饋</h2>
      <form onSubmit={submit} className="stack">
        <fieldset>
          <legend>
            整體滿意度 <span className="required">必填</span>
          </legend>
          <div className="stars" role="radiogroup">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                type="button"
                key={n}
                role="radio"
                aria-checked={rating === n}
                aria-label={`${n} 顆星`}
                className={rating && n <= rating ? 'on' : ''}
                onClick={() => setRating(n)}
              >
                ★
              </button>
            ))}
          </div>
        </fieldset>
        <fieldset>
          <legend>夾取力道</legend>
          <div className="segmented">
            {GRIP_OPTIONS.map((o) => (
              <label key={o.value} className={grip === o.value ? 'on' : ''}>
                <input type="radio" name="grip" value={o.value} checked={grip === o.value} onChange={() => setGrip(o.value)} />
                {o.label}
              </label>
            ))}
          </div>
        </fieldset>
        <label className="stack">
          <span>其他意見</span>
          <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={3} maxLength={4000} placeholder="例如：接近杯子時太快" />
        </label>
        <button type="submit" className="primary" disabled={busy || !rating}>
          {busy ? '送出中…' : rating ? '送出回饋' : '請先選擇滿意度'}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
    </section>
  )
}
