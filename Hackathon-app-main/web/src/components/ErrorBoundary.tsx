import { Component, type ReactNode } from 'react'
import { clearChat } from '../lib/store'

/** Keeps one broken view from blanking the whole app, and offers a way out. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="page">
        <section className="card">
          <h2>畫面出錯了</h2>
          <pre className="error-box">{this.state.error.message}</pre>
          <p className="muted">可以先重新載入；如果還是一樣，清除本機的對話紀錄通常就能恢復（雲端的對話不受影響）。</p>
          <div className="row">
            <button onClick={() => location.reload()}>重新載入</button>
            <button
              className="danger"
              onClick={() => {
                clearChat()
                location.reload()
              }}
            >
              清除對話紀錄並重新載入
            </button>
          </div>
        </section>
      </div>
    )
  }
}
