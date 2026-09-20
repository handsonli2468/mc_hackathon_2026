import { useEffect, useState } from 'react'

/** MJPEG stream from the gateway; retries every few seconds while no frame is available. */
export function MjpegView({ stream, alt }: { stream: 'live' | 'calib' | 'camera'; alt: string }) {
  const [attempt, setAttempt] = useState(0)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!failed) return
    const t = setTimeout(() => {
      setFailed(false)
      setAttempt((a) => a + 1)
    }, 3000)
    return () => clearTimeout(t)
  }, [failed])

  return (
    <div className="video">
      {failed ? (
        <div className="video-empty">
          <span>尚無影像</span>
          <small>{stream === 'camera' ? '確認 RealSense 相機節點是否已啟動' : '確認 field_calib_node 是否已啟動'}，3 秒後重試…</small>
        </div>
      ) : (
        <img key={`${stream}-${attempt}`} src={`/api/calib/stream/${stream}?n=${attempt}`} alt={alt} onError={() => setFailed(true)} />
      )}
    </div>
  )
}
