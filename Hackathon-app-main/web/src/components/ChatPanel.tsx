import { useEffect, useRef, useState } from 'react'
import type { ChatReply } from '../api/types'
import type { ChatMessage } from '../lib/store'
import { PlanSteps } from './PlanSteps'

const STATUS_LABEL: Record<string, string> = {
  SUCCESS: '已產生行為樹',
  NEED_MORE_INFO: '需要補充資訊',
  PLANNING_FAILURE: '規劃失敗',
  UNSUPPORTED: '機器人做不到',
  UNSAFE: '判定為不安全',
  INVALID_REQUEST: '指令無效',
}

/** Replies saved by older versions may still hold objects such as {field, question}. */
function text(item: unknown): string {
  if (typeof item === 'string') return item
  if (item && typeof item === 'object') {
    const o = item as Record<string, unknown>
    for (const k of ['question', 'message', 'description', 'name', 'text']) if (typeof o[k] === 'string') return o[k] as string
  }
  return JSON.stringify(item)
}

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  return <>{Math.round((now - since) / 1000)} 秒</>
}

function AgentBubble({ reply }: { reply: ChatReply }) {
  // Replies saved before auto-execution had `executable` instead of bt_generated/execution.
  const legacy = reply as ChatReply & { executable?: boolean }
  const generated = reply.bt_generated ?? !!legacy.executable
  const ex = reply.execution
  if (generated) {
    return (
      <>
        {ex?.status === 'STARTED' ? (
          <div className="ok-text">
            已產生行為樹，機器人開始執行{ex.run_id ? `（${ex.run_id}）` : ''}。
            {ex.preempted_previous && <small className="warn-text"> 已中斷先前的任務。</small>}
          </div>
        ) : ex?.status === 'FAILED' ? (
          <div className="error">已產生行為樹，但送到機器人失敗：{text(ex.error ?? ex.reason ?? '沒有說明')}</div>
        ) : ex?.status === 'SKIPPED' ? (
          <div>已產生行為樹，但這次沒有執行：{text(ex.reason ?? '沒有說明')}</div>
        ) : (
          <div>已產生行為樹。</div>
        )}
        {reply.steps && <PlanSteps reply={reply} />}
      </>
    )
  }
  return (
    <>
      <div>{text(reply.message) || '（沒有說明）'}</div>
      {reply.questions.length > 0 && (
        <ul className="questions">
          {reply.questions.map((q, i) => (
            <li key={i}>{text(q)}</li>
          ))}
        </ul>
      )}
      {reply.missing_capabilities.length > 0 && (
        <small className="muted">機器人缺少的能力：{reply.missing_capabilities.map(text).join('、')}</small>
      )}
      {reply.status && reply.status !== 'NEED_MORE_INFO' && (
        <small className="muted">狀態：{STATUS_LABEL[reply.status] ?? reply.status}</small>
      )}
    </>
  )
}

export function ChatPanel({
  messages,
  thinkingSince,
  onSend,
  onReset,
  draft,
  setDraft,
}: {
  messages: ChatMessage[]
  thinkingSince: number | null
  onSend: (text: string) => void
  onReset: () => void
  draft: string
  setDraft: (s: string) => void
}) {
  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages.length, thinkingSince])

  const busy = thinkingSince !== null
  function submit(e: React.FormEvent) {
    e.preventDefault()
    const text = draft.trim()
    if (text && !busy) onSend(text)
  }

  return (
    <section className="card">
      <div className="row between">
        <h2>和機器人對話</h2>
        <button className="link" onClick={onReset} disabled={busy || messages.length === 0}>
          開新對話
        </button>
      </div>
      <div className="chat" ref={listRef}>
        {messages.length === 0 && <p className="muted">說說看要機器人做什麼，例如「找到桌上的紅色杯子，把它夾起來」。</p>}
        {messages.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            {m.role === 'user' ? m.text : m.reply ? <AgentBubble reply={m.reply} /> : m.text}
          </div>
        ))}
        {busy && (
          <div className="bubble agent thinking">
            <span className="spinner" /> 規劃中，通常要 30–60 秒，成功後會直接開始執行…（<Elapsed since={thinkingSince} />）
          </div>
        )}
      </div>
      <form onSubmit={submit} className="chat-input">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) submit(e)
          }}
          placeholder={messages.length ? '回覆或補充說明…' : '輸入任務指令'}
          rows={2}
          maxLength={4000}
          disabled={busy}
        />
        <button type="submit" className="primary" disabled={busy || !draft.trim()}>
          送出
        </button>
      </form>
      <p className="muted hint">規劃成功後，機器人會<b>直接開始執行</b>；隨時可以在下方按「中斷任務」。</p>
    </section>
  )
}
