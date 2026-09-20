import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, api } from '../api/client'
import type { CalibResult, CalibRunResponse, FieldConfig } from '../api/types'

export type CalibStep = 'field' | 'capture' | 'result'

interface CalibrationState {
  step: CalibStep
  field: FieldConfig | null
  current: CalibResult | null // the applied calibration
  run: CalibRunResponse | null // result of the last run started here
  running: boolean
  error: string
  setStep: (s: CalibStep) => void
  setField: (f: FieldConfig) => void
  saveField: () => Promise<void>
  start: () => Promise<void>
}

const Ctx = createContext<CalibrationState | null>(null)
const KEY = 'hackathon-app:calibration'

interface Saved {
  step: CalibStep
  run: CalibRunResponse | null
}

function load(): Saved {
  try {
    const s = JSON.parse(localStorage.getItem(KEY) ?? 'null') as Saved | null
    if (s && ['field', 'capture', 'result'].includes(s.step)) return s
  } catch {
    /* storage unavailable or corrupt */
  }
  return { step: 'field', run: null }
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/**
 * Lives above the routes, so the flow (and a calibration in progress) survives switching tabs.
 * Step and last result are also kept in localStorage for reloads.
 */
export function CalibrationProvider({ children }: { children: ReactNode }) {
  const saved = useRef(load()).current
  const [step, setStep] = useState<CalibStep>(saved.run || saved.step !== 'result' ? saved.step : 'field')
  const [run, setRun] = useState<CalibRunResponse | null>(saved.run)
  const [field, setField] = useState<FieldConfig | null>(null)
  const [current, setCurrent] = useState<CalibResult | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify({ step, run } satisfies Saved))
    } catch {
      /* storage unavailable */
    }
  }, [step, run])

  useEffect(() => {
    api.getField().then(setField, (e) => setError(errText(e)))
    api.calibResult().then(setCurrent, () => setCurrent(null))
    api.calibState().then(
      (s) => {
        if (s.error) setError(`校正功能目前停用（Gateway 讀寫不到定位 server 的檔案）：${s.error}`)
        // Started before a reload (or from another device): follow it until it ends.
        if (s.running_since) followRemoteRun()
      },
      () => undefined,
    )
  }, [])

  async function followRemoteRun() {
    setRunning(true)
    setStep('capture')
    try {
      for (;;) {
        await new Promise((r) => setTimeout(r, 2000))
        const s = await api.calibState()
        if (!s.running_since) break
      }
      setRun(await api.calibLatestRun())
      api.calibResult().then(setCurrent, () => undefined)
      setStep('result')
    } catch (e) {
      setError(errText(e))
    } finally {
      setRunning(false)
    }
  }

  async function saveField() {
    if (!field) return
    setError('')
    try {
      setField(await api.putField(field))
      setStep('capture')
    } catch (e) {
      setError(errText(e))
    }
  }

  async function start() {
    setRunning(true)
    setError('')
    try {
      const res = await api.calibRun()
      setRun(res)
      if (res.success) api.calibResult().then(setCurrent, () => undefined)
      setStep('result')
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? '已經有校正正在進行，請稍候。' : errText(e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <Ctx.Provider value={{ step, field, current, run, running, error, setStep, setField, saveField, start }}>
      {children}
    </Ctx.Provider>
  )
}

export function useCalibration(): CalibrationState {
  const c = useContext(Ctx)
  if (!c) throw new Error('useCalibration outside CalibrationProvider')
  return c
}
