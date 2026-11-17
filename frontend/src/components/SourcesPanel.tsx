import { useRef, useState } from 'react'
import { upload } from '../lib/api'
import { asks, getOperator } from '../lib/ask'
import { ago } from '../lib/format'
import { useResource } from '../lib/hooks'
import { loadSourceRows, modeLabel, type SourceRow, type SourceTableRow } from '../lib/sources'
import { useAsk } from './AskPanel'
import { Empty, ErrorNote, Loading, Section, StatusPill } from './ui'

/* Phase 15 (SRC-10, SRC-08): every source with its tables, modes, last sync,
 * freshness and backfill. Add source and Upload a CSV only message the agent;
 * a source exists only after an approved source_change. */

/* Quick 027: the legacy row is name 'business_db' with kind 'postgres' (schema.sql seed);
 * kind alone never says business_db. Keep the kind test for older fixtures. */
export function isBusinessDb(s: Pick<SourceRow, 'name' | 'kind'>): boolean {
  return s.name === 'business_db' || s.kind === 'business_db'
}

function freshness(s: SourceRow): string {
  if (isBusinessDb(s)) return 'live (business database)'
  if (s.freshness_minutes == null) return 'not tracked'
  return s.stale ? 'stale' : 'fresh'
}

function lastSync(s: SourceRow): string {
  if (isBusinessDb(s)) return 'live (business database)'
  if (!s.last_run) return 'last sync never'
  const when = ago(s.last_run.ended_at ?? s.last_run.started_at)
  return `last sync ${when}: ${s.last_run.status}`
}

function tableLine(t: SourceTableRow): string {
  if (t.mode === 'live' && !t.pending_mode) return 'live'
  if (!t.backfill_done && t.backfill_progress != null) return `backfilling ${Math.round(t.backfill_progress * 100)}%`
  return t.last_success_at ? `synced ${ago(t.last_success_at)}` : 'never synced'
}

export function SourcesPanelView({
  sources,
  onAddSource,
  onUploadCsv,
  note,
  fileRef,
}: {
  sources: SourceRow[]
  onAddSource?: () => void
  onUploadCsv?: () => void
  note?: string | null
  fileRef?: React.RefObject<HTMLInputElement>
}) {
  return (
    <Section title="Sources and sync">
      <div className="btn-row">
        <button className="btn btn-sm" onClick={onAddSource}>
          Add source
        </button>
        <label className="ic-meta">
          Upload a CSV <input className="input" type="file" accept=".csv,text/csv" ref={fileRef} onChange={onUploadCsv} />
        </label>
      </div>
      {note && <div className="banner">{note}</div>}
      {sources.length === 0 ? (
        <Empty title="No sources yet">Add a source and the agent will walk you through it.</Empty>
      ) : (
        <div className="ic-list">
          {sources.map((s) => {
            const gold = !!(s.stale || s.failing || s.unreachable)
            return (
              <div className={`ic-row${gold ? ' ic-gold' : ''}`} key={s.name}>
                <span>
                  <strong>{s.name}</strong> <span className="ic-meta">{s.kind}</span> <StatusPill status={s.status} />
                </span>
                <span className="ic-meta">
                  {lastSync(s)} · freshness {freshness(s)}
                </span>
                {s.stale && <span className="ic-owed-line">out of date</span>}
                {s.failing && (
                  <span className="ic-owed-line">last two syncs failed: {s.last_run?.error ?? 'no detail'}</span>
                )}
                {s.unreachable && <span className="ic-owed-line">live tables cannot be reached</span>}
                {s.tables.map((t) => (
                  <span className="ic-meta" key={`${t.warehouse_schema}.${t.warehouse_table}`}>
                    {t.warehouse_schema}.{t.warehouse_table} <span className="ic-badge">{t.pending_mode ? modeLabel(t) : t.mode}</span> {tableLine(t)}
                    {t.last_error && <span className="ic-owed-line"> last error: {t.last_error}</span>}
                  </span>
                ))}
              </div>
            )
          })}
        </div>
      )}
    </Section>
  )
}

export default function SourcesPanel() {
  const { send, open } = useAsk()
  const fileRef = useRef<HTMLInputElement>(null)
  const [note, setNote] = useState<string | null>(null)
  const res = useResource(loadSourceRows, { interval: 30_000 })
  const told = 'The agent will reply in the Ask panel. Nothing is created until you approve it.'

  const addSource = async () => {
    await send(asks.addSource())
    setNote(told)
    open()
  }
  const uploadCsv = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    try {
      const form = new FormData()
      form.append('file', file, file.name)
      form.append('operator', getOperator())
      form.append('instruction', asks.csvSource(file.name))
      await upload('/api/documents/upload', form)
      setNote(told)
      if (fileRef.current) fileRef.current.value = ''
      open()
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Upload failed')
    }
  }
  if (res.loading && !res.data) return <Loading label="Reading sources" />
  return (
    <>
      {res.error && <ErrorNote message={res.error} onRetry={() => void res.reload()} />}
      <SourcesPanelView
        sources={res.data ?? []}
        note={note}
        fileRef={fileRef}
        onAddSource={() => void addSource()}
        onUploadCsv={() => void uploadCsv()}
      />
    </>
  )
}
