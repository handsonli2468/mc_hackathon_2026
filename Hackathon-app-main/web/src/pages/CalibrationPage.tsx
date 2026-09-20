import { useEffect, useState } from 'react'
import type { CalibResult, FieldConfig } from '../api/types'
import { MjpegView } from '../components/MjpegView'
import { type CalibStep, useCalibration } from '../lib/calibration'

// Thresholds from field_calib param.yaml (calib.min_accept_ratio / calib.max_rms_px).
const MIN_ACCEPT_RATIO = 0.6
const MAX_RMS_PX = 1.5

function segmentNames(count: number): string[] {
  const names = ['far']
  for (let i = 0; i < count; i++) names.push(`right_${i}`, `left_${i}`)
  names.push('near')
  for (let i = 1; i < count; i++) names.push(`seam_${i}`)
  return names
}

const SEGMENT_LABEL: Record<string, string> = { far: '遠側長邊', near: '近側長邊' }
function segmentLabel(name: string): string {
  if (SEGMENT_LABEL[name]) return SEGMENT_LABEL[name]
  const [kind, idx] = name.split('_')
  const n = Number(idx)
  if (kind === 'left') return `第 ${n + 1} 張左短邊`
  if (kind === 'right') return `第 ${n + 1} 張右短邊`
  if (kind === 'seam') return `第 ${n}/${n + 1} 張接縫`
  return name
}

const deg = (rad: number) => ((rad * 180) / Math.PI).toFixed(2)

const STEPS: { key: CalibStep; label: string }[] = [
  { key: 'field', label: '桌子資訊' },
  { key: 'capture', label: '校正畫面' },
  { key: 'result', label: '校正結果' },
]

/** One progress bar for the three steps; informative only, not clickable. */
function StepProgress({ step }: { step: CalibStep }) {
  const idx = STEPS.findIndex((s) => s.key === step)
  return (
    <div className="progress" role="progressbar" aria-valuemin={1} aria-valuemax={STEPS.length} aria-valuenow={idx + 1}
      aria-valuetext={`步驟 ${idx + 1}／${STEPS.length}：${STEPS[idx].label}`}>
      <div className="progress-head">
        <b>{STEPS[idx].label}</b>
        <span className="muted">步驟 {idx + 1}／{STEPS.length}</span>
      </div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${((idx + 1) / STEPS.length) * 100}%` }} />
      </div>
      <ol className="progress-labels">
        {STEPS.map((s, i) => (
          <li key={s.key} className={i < idx ? 'done' : i === idx ? 'on' : ''}>
            {s.label}
          </li>
        ))}
      </ol>
    </div>
  )
}

const VIEWS: { key: 'camera' | 'overlay'; label: string }[] = [
  { key: 'camera', label: '相機畫面（即時）' },
  { key: 'overlay', label: '桌緣疊圖' },
]

export function CalibrationPage() {
  const { step, field, current, run, running, error, setStep, setField, saveField, start } = useCalibration()
  const [view, setView] = useState<'camera' | 'overlay'>(running ? 'overlay' : 'camera')
  // The detection overlay is what matters while calibrating; the camera view is for framing.
  useEffect(() => {
    setView(running ? 'overlay' : 'camera')
  }, [running])

  return (
    <div className="page">
      <StepProgress step={step} />

      {error && <p className="error">{error}</p>}

      {step === 'field' && (
        <section className="card">
          <h2>輸入桌子（場地）資訊</h2>
          {!field ? (
            <p className="muted">載入中…</p>
          ) : (
            <FieldForm
              field={field}
              onChange={setField}
              onSubmit={(e) => {
                e.preventDefault()
                void saveField()
              }}
            />
          )}
          {current && <CurrentSummary result={current} />}
        </section>
      )}

      {step === 'capture' && (
        <section className="card">
          <h2>校正畫面</h2>
          <p>
            請確認<b>整張桌子都在相機畫面內</b>，桌面上盡量不要放東西、也不要有人手遮住桌緣。
            {field && (
              <span className="muted">
                （{field.count} 張 {field.length} × {field.depth} m）
              </span>
            )}
          </p>
          <div className="segmented view-toggle" role="radiogroup" aria-label="畫面">
            {VIEWS.map((v) => (
              <label key={v.key} className={view === v.key ? 'on' : ''}>
                <input type="radio" name="calib-view" checked={view === v.key} onChange={() => setView(v.key)} />
                {v.label}
              </label>
            ))}
          </div>
          <MjpegView
            stream={view === 'camera' ? 'camera' : running ? 'calib' : 'live'}
            alt={view === 'camera' ? '相機即時畫面' : running ? '校正中偵測到的桌緣' : '桌緣疊圖'}
          />
          <p className="muted legend">
            {view === 'camera'
              ? '相機即時畫面：用來確認整張桌子都在畫面裡。'
              : running
                ? '校正中：每一輪會縮小搜尋範圍。綠點是採用的桌緣點，其他顏色是捨棄的點（對比不足、落在範圍邊界等）；灰線為本輪前的模型，彩色線為本輪結果。'
                : '桌緣疊圖：線條為依目前外參投影的桌緣，應與實際桌緣重合。由校正程式計算，更新速度取決於 field_calib 的設定。'}
            {running && ' 切換到其他頁面不會中斷校正。'}
          </p>
          <div className="row between">
            <button onClick={() => setStep('field')} disabled={running}>
              ← 修改桌子資訊
            </button>
            <button className="primary" onClick={() => void start()} disabled={running}>
              {running ? (
                <>
                  <span className="spinner" /> 校正中…
                </>
              ) : (
                '開始校正'
              )}
            </button>
          </div>
        </section>
      )}

      {step === 'result' && run && (
        <section className="card">
          <div className="row between">
            <h2>校正結果</h2>
            <span className={`badge ${run.success ? 'badge-success' : 'badge-failure'}`}>{run.success ? '通過' : '未通過'}</span>
          </div>
          {!run.success && <p>結果未套用，仍沿用先前的外參。可以依下方問題調整後重試。</p>}
          {run.result ? <ResultDetails result={run.result} runName={run.run} /> : <pre className="error-box">{run.message}</pre>}
          {run.message && (
            <details>
              <summary>校正程式完整訊息</summary>
              <pre className="log">{run.message}</pre>
            </details>
          )}
          <div className="row between">
            <button onClick={() => setStep('field')}>修改桌子資訊</button>
            <button className="primary" onClick={() => setStep('capture')}>
              重新校正
            </button>
          </div>
        </section>
      )}
    </div>
  )
}

function FieldForm({
  field,
  onChange,
  onSubmit,
}: {
  field: FieldConfig
  onChange: (f: FieldConfig) => void
  onSubmit: (e: React.FormEvent) => void
}) {
  const segments = segmentNames(field.count)
  const toggle = (name: string) =>
    onChange({
      ...field,
      disabled_segments: field.disabled_segments.includes(name)
        ? field.disabled_segments.filter((s) => s !== name)
        : [...field.disabled_segments, name],
    })

  return (
    <form onSubmit={onSubmit} className="stack">
      <div className="grid3">
        <label>
          <span>單張長度（m）</span>
          <input type="number" step="0.01" min="0.2" max="10" required value={field.length}
            onChange={(e) => onChange({ ...field, length: Number(e.target.value) })} />
        </label>
        <label>
          <span>單張寬度（m）</span>
          <input type="number" step="0.01" min="0.2" max="10" required value={field.depth}
            onChange={(e) => onChange({ ...field, depth: Number(e.target.value) })} />
        </label>
        <label>
          <span>桌子數量</span>
          <input type="number" step="1" min="1" max="10" required value={field.count}
            onChange={(e) => {
              const count = Math.max(1, Math.round(Number(e.target.value)))
              const valid = new Set(segmentNames(count))
              onChange({ ...field, count, disabled_segments: field.disabled_segments.filter((s) => valid.has(s)) })
            }} />
        </label>
      </div>
      <p className="muted">多張桌子沿著寬度方向並排（長邊相接）。整體場地：{field.length} × {(field.depth * field.count).toFixed(2)} m</p>
      <details>
        <summary>忽略被擋住的桌緣（選填）</summary>
        <div className="chips">
          {segments.map((s) => (
            <label key={s} className={field.disabled_segments.includes(s) ? 'chip on' : 'chip'}>
              <input type="checkbox" checked={field.disabled_segments.includes(s)} onChange={() => toggle(s)} />
              {segmentLabel(s)}
            </label>
          ))}
        </div>
      </details>
      <button type="submit" className="primary">
        儲存並前往校正畫面 →
      </button>
    </form>
  )
}

function CurrentSummary({ result }: { result: CalibResult }) {
  return (
    <p className="muted current">
      目前生效的校正：{result.calibrated_at}（{result.table.count} 張 {result.table.length} × {result.table.depth} m，
      誤差 {result.quality.rms_px.toFixed(2)} px）
    </p>
  )
}

function Metric({ label, value, ok, hint }: { label: string; value: string; ok: boolean; hint: string }) {
  return (
    <div className={`metric ${ok ? 'm-ok' : 'm-bad'}`}>
      <span className="m-label">{label}</span>
      <span className="m-value">{value}</span>
      <span className="m-hint">{hint}</span>
    </div>
  )
}

function ResultDetails({ result, runName }: { result: CalibResult; runName: string | null }) {
  const q = result.quality
  const tf = result.cam_tf
  return (
    <div className="stack">
      <div className="metrics">
        <Metric label="採用率" value={`${(q.accept_ratio * 100).toFixed(1)}%`} ok={q.accept_ratio >= MIN_ACCEPT_RATIO} hint={`需 ≥ ${MIN_ACCEPT_RATIO * 100}%`} />
        <Metric label="重投影誤差" value={`${q.rms_px.toFixed(2)} px`} ok={q.rms_px <= MAX_RMS_PX} hint={`需 ≤ ${MAX_RMS_PX} px`} />
        <Metric label="相機高度" value={`${tf.z.toFixed(3)} m`} ok hint={q.depth_plane?.height_depth ? `深度量測 ${q.depth_plane.height_depth.toFixed(3)} m` : ''} />
      </div>

      {result.problems.length > 0 && (
        <div className="failure">
          <h3>發現的問題</h3>
          <ul>
            {result.problems.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      )}

      {runName && <img className="result-img" src={`/api/calib/image/final_overlay?run=${runName}`} alt="最終桌緣疊圖" />}

      <details>
        <summary>相機外參（map → camera_link）</summary>
        <table className="kv">
          <tbody>
            <tr><th>x / y / z</th><td>{tf.x.toFixed(3)} / {tf.y.toFixed(3)} / {tf.z.toFixed(3)} m</td></tr>
            <tr><th>roll / pitch / yaw</th><td>{deg(tf.roll)}° / {deg(tf.pitch)}° / {deg(tf.yaw)}°</td></tr>
            <tr><th>桌子</th><td>{result.table.count} 張 {result.table.length} × {result.table.depth} m</td></tr>
            <tr><th>時間</th><td>{result.calibrated_at}</td></tr>
          </tbody>
        </table>
      </details>

      <details>
        <summary>各桌緣品質</summary>
        <table className="segments">
          <thead>
            <tr><th>桌緣</th><th>採用 / 取樣</th><th>RMS (px)</th></tr>
          </thead>
          <tbody>
            {Object.entries(q.segments).map(([name, s]) => (
              <tr key={name} className={s.samples && s.accepted / s.samples < MIN_ACCEPT_RATIO ? 'm-bad' : ''}>
                <td>{segmentLabel(name)}</td>
                <td>{s.accepted} / {s.samples}</td>
                <td>{s.rms_px.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  )
}
