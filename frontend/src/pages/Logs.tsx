import { useEffect, useRef, useState } from 'react'
import { streamEvents } from '../lib/api'
import { useStored } from '../lib/hooks'
import type { LogEntry } from '../lib/types'
import { PageHeader, Section } from '../components/ui'
import './logs.css'

/* The gateway's live log. Streamed over SSE, read through fetch rather than
 * EventSource because the stream is ops-key gated and EventSource cannot set a
 * header. */

const MAX_LINES = 600
const LEVELS = ['ALL', 'INFO', 'WARNING', 'ERROR'] as const
type Level = (typeof LEVELS)[number]

export default function Logs() {
  const [lines, setLines] = useState<LogEntry[]>([])
  const [status, setStatus] = useState<'connecting' | 'live' | 'stopped'>('connecting')
  const [follow, setFollow] = useStored('logs.follow', true)
  const [level, setLevel] = useStored<Level>('logs.level', 'ALL')
  const [filter, setFilter] = useState('')
  const paneRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    setStatus('connecting')
    streamEvents(
      '/api/logs/stream',
      (data) => {
        setStatus('live')
        setLines((prev) => {
          const next = [...prev, data as LogEntry]
          return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next
        })
      },
      ctrl.signal,
    ).catch(() => {
      if (!ctrl.signal.aborted) setStatus('stopped')
    })
    return () => ctrl.abort()
  }, [])

  useEffect(() => {
    if (follow) paneRef.current?.scrollTo({ top: paneRef.current.scrollHeight })
  }, [lines, follow])

  const shown = lines.filter((l) => {
    if (level !== 'ALL' && l.level !== level) return false
    if (!filter) return true
    const needle = filter.toLowerCase()
    return l.msg?.toLowerCase().includes(needle) || l.logger?.toLowerCase().includes(needle)
  })

  return (
    <>
      <PageHeader
        title="Logs"
        lede="What the gateway is doing right now — every loop, every send, every failure."
      />

      <Section
        title="Live"
        meta={
          status === 'live'
            ? `${shown.length} of ${lines.length} lines`
            : status === 'connecting'
              ? 'connecting'
              : 'disconnected — reload to reconnect'
        }
        actions={
          <div className="row wrap" style={{ gap: 'var(--s2)' }}>
            <input
              className="input"
              style={{ width: 180, minHeight: 28, padding: '3px 8px' }}
              placeholder="Filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <select
              className="select"
              style={{ width: 120, minHeight: 28, padding: '3px 8px' }}
              value={level}
              onChange={(e) => setLevel(e.target.value as Level)}
            >
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l === 'ALL' ? 'All levels' : l}
                </option>
              ))}
            </select>
            <label className="check">
              <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
              Follow
            </label>
          </div>
        }
        flush
      >
        <div className="logpane" ref={paneRef}>
          {shown.length === 0 ? (
            <div className="dimmer" style={{ padding: 'var(--s4)' }}>
              {status === 'live' ? 'Nothing yet.' : 'Waiting for the stream…'}
            </div>
          ) : (
            shown.map((l, i) => (
              <div className="logline" key={i}>
                <span className="log-ts">{l.ts}</span>
                <span className={`log-lv lv-${l.level}`}>{l.level}</span>
                <span className="log-src">{l.logger}</span>
                <span className="log-msg">{l.msg}</span>
              </div>
            ))
          )}
        </div>
      </Section>
    </>
  )
}
