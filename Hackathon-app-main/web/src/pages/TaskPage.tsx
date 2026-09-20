import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, api, latestStatus } from '../api/client'
import type { BtStatus, RunSummary } from '../api/types'
import { ChatPanel } from '../components/ChatPanel'
import { RunTimeline } from '../components/RunTimeline'
import { StatusBadge } from '../components/StatusBadge'
import { type ChatState, loadChat, rememberPrompt, saveChat } from '../lib/store'
import { usePolling } from '../lib/usePolling'

/** A mission Manta started (auto_execution); followed on bt_engine by run_id. */
interface Tracking {
  missionId: string
  prompt: string
  runId: string
}

/** Hand-off from the feedback page: a message to send in the current conversation. */
export interface TaskPageState {
  send?: string
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export function TaskPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const [chat, setChatState] = useState<ChatState>(loadChat)
  const [draft, setDraft] = useState('')
  const [thinkingSince, setThinkingSince] = useState<number | null>(null)
  const [tracking, setTracking] = useState<Tracking | null>(null)
  const [status, setStatus] = useState<BtStatus | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [statusError, setStatusError] = useState('')
  const handedOff = useRef(false)

  function setChat(next: ChatState) {
    setChatState(next)
    saveChat(next)
  }

  async function send(text: string) {
    // A generated tree starts right away and preempts whatever is running.
    if (status?.state === 'running' && !confirm('機器人正在執行任務。如果這則訊息規劃成功，會中斷目前的任務並開始新的。確定送出？')) {
      return
    }
    setError('')
    setInfo('')
    const withUser: ChatState = { ...chat, messages: [...chat.messages, { role: 'user', text }] }
    setChat(withUser)
    setDraft('')
    setThinkingSince(Date.now())
    try {
      const reply = await api.chat(text, chat.sessionId)
      setChat({
        sessionId: reply.session_id ?? chat.sessionId,
        messages: [...withUser.messages, { role: 'agent', text: reply.message, reply }],
      })
      const ex = reply.execution
      if (ex?.status === 'STARTED' && ex.run_id && reply.mission_id) {
        rememberPrompt(ex.run_id, text)
        setTracking({ missionId: reply.mission_id, prompt: text, runId: ex.run_id })
      } else if (reply.bt_generated && ex?.status === 'FAILED') {
        setError(`行為樹已產生，但送到機器人失敗：${ex.error ?? ex.reason ?? '沒有說明'}`)
      }
    } catch (e) {
      // /api/chat is not idempotent: a timed-out request may still have started the robot.
      setError(
        e instanceof ApiError && (e.status === 504 || e.status === 0)
          ? '雲端沒有及時回應。任務可能仍在規劃，或已經開始執行：請先看下方「任務狀態」，確認沒有在跑再重送。'
          : `雲端回應錯誤：${errText(e)}`,
      )
      setDraft(text)
    } finally {
      setThinkingSince(null)
    }
  }

  // Retry from the feedback page: send the edited prompt in the same conversation, once.
  useEffect(() => {
    const st = location.state as TaskPageState | null
    if (st?.send && !handedOff.current) {
      handedOff.current = true
      navigate('.', { replace: true, state: null })
      void send(st.send)
    }
  }, [location.state])

  async function reset() {
    if (!confirm('開一個新的對話？目前的對話紀錄會清空。')) return
    try {
      const r = await api.resetSession(chat.sessionId)
      setChat({ sessionId: r.session_id, messages: [] })
    } catch (e) {
      setError(errText(e))
      return
    }
    setInfo('')
  }

  const active = !!tracking || status?.state === 'running'

  usePolling(
    async () => {
      try {
        const s = tracking ? await api.btStatus(tracking.runId) : await latestStatus()
        setStatus(s)
        setStatusError('')
        if (s && tracking?.runId === s.run_id && s.state !== 'running') {
          setTracking(null)
          navigate(`/feedback/${encodeURIComponent(s.run_id)}`)
        }
      } catch (e) {
        setStatusError(errText(e))
      }
    },
    active ? 500 : 3000,
  )

  usePolling(async () => {
    try {
      setRuns((await api.btRuns()).slice().reverse().slice(0, 8))
    } catch {
      /* shown by the status poll */
    }
  }, 5000)

  async function cancel() {
    if (!confirm('確定要中斷目前的任務？機器人會停下所有動作。')) return
    try {
      const r = tracking ? await api.cancelMission(tracking.missionId) : await api.btCancel()
      setInfo(r.was_running === false ? '目前沒有執行中的任務。' : '已送出中斷。')
    } catch (e) {
      setError(errText(e))
    }
  }

  return (
    <div className="page">
      <ChatPanel
        messages={chat.messages}
        thinkingSince={thinkingSince}
        onSend={send}
        onReset={reset}
        draft={draft}
        setDraft={setDraft}
      />

      {(info || error) && (
        <div>
          {info && <p className="info">{info}</p>}
          {error && <p className="error">{error}</p>}
        </div>
      )}

      <section className="card">
        <div className="row between">
          <h2>任務狀態</h2>
          <button className="danger" onClick={cancel} disabled={status?.state !== 'running'}>
            中斷任務
          </button>
        </div>
        {statusError && <p className="error">{statusError}</p>}
        {!status && !statusError && <p className="muted">還沒有任何任務紀錄。</p>}
        {status && (
          <>
            <div className="row status-line">
              <StatusBadge state={status.state} />
              <code>{status.run_id}</code>
              <span className="muted">{status.elapsed_s?.toFixed(1)} 秒</span>
            </div>
            {status.state === 'running' && (
              <p>
                目前動作：<b>{status.running_leaves.length ? status.running_leaves.join('、') : '—'}</b>
              </p>
            )}
            {status.state !== 'running' && (
              <Link to={`/feedback/${encodeURIComponent(status.run_id)}`}>查看結果與回饋 →</Link>
            )}
            <details open={status.state === 'running'}>
              <summary>執行紀錄</summary>
              <RunTimeline trace={status.trace ?? []} truncated={status.trace_truncated} />
            </details>
          </>
        )}
      </section>

      {runs.length > 0 && (
        <section className="card">
          <h2>最近的任務</h2>
          <ul className="runs">
            {runs.map((r) => (
              <li key={r.run_id}>
                <Link to={`/feedback/${encodeURIComponent(r.run_id)}`}>
                  <code>{r.run_id}</code>
                  <StatusBadge state={r.state} />
                  <span className="muted">{new Date(r.started_at * 1000).toLocaleTimeString('zh-TW')}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
