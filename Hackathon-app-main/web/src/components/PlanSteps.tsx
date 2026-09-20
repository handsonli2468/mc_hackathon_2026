import type { ChatReply } from '../api/types'

const ACTION_LABEL: Record<string, string> = {
  VisualizeObject: '用相機尋找物品',
  IsObjectFound: '確認是否找到',
  NavigateToDetectedObject: '移動到物品旁',
  NavigateToPoint: '移動到指定座標',
  TrackObject: '靠近物品',
  RotateInPlace: '原地旋轉',
  SetGripper: '控制夾爪',
  Patrol: '巡邏搜尋',
}

function describeArgs(args: Record<string, unknown>): string {
  return Object.entries(args)
    .map(([k, v]) => (k === 'position' ? `夾爪 ${v}/100` : `${k}=${String(v)}`))
    .join('、')
}

/** The planned mission, read-only: Manta starts it by itself once the tree is generated. */
export function PlanSteps({ reply }: { reply: ChatReply }) {
  return (
    <details className="plan-details">
      <summary>規劃步驟{reply.steps.length ? `（${reply.steps.length} 步）` : ''}</summary>
      {reply.goal && <p className="plan-goal">目標：{reply.goal}</p>}
      {reply.steps.length > 0 ? (
        <ol className="plan">
          {reply.steps.map((s, i) => (
            <li key={i}>
              <b>{ACTION_LABEL[s.action] ?? s.action}</b>
              {s.objective && <span className="muted">：{s.objective}</span>}
              {Object.keys(s.arguments).length > 0 && <small className="muted"> （{describeArgs(s.arguments)}）</small>}
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">雲端沒有提供步驟摘要。</p>
      )}
      {reply.bt_xml && (
        <details>
          <summary>行為樹 XML</summary>
          <pre className="log">{reply.bt_xml}</pre>
        </details>
      )}
    </details>
  )
}
