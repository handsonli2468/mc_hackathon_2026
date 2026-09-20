import { useEffect, useRef } from 'react'

/** Calls fn every intervalMs while enabled; waits for each call before scheduling the next. */
export function usePolling(fn: () => Promise<void> | void, intervalMs: number, enabled = true): void {
  const ref = useRef(fn)
  ref.current = fn

  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      try {
        await ref.current()
      } finally {
        if (!stopped) timer = setTimeout(tick, intervalMs)
      }
    }
    void tick()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [intervalMs, enabled])
}
