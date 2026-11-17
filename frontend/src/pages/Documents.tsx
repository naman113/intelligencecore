import { useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { asObj, get, upload } from '../lib/api'
import { ago, isoTitle, titleCase, truncate } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import type { DocumentDetail, DocumentRow, ExtractedField } from '../lib/types'
import { ExtractedTable } from '../components/ActionPayload'
import { Async, Empty, FormAction, KeyValue, PageHeader, Section, StatusPill } from '../components/ui'
import { ChevronLeft, Upload } from '../components/icons'
import '../components/split.css'

/* Invoices, quotations and agreements arrive three ways — a WhatsApp
 * attachment, the watched inbox folder, or the upload here. All three take the
 * same path: read it, propose the fields, wait for a person to approve. */

const ACCEPT = '.pdf,.jpg,.jpeg,.png,.webp,.xlsx,.docx,.csv,.txt,.xls'

export default function Documents() {
  const { id } = useParams()
  const navigate = useNavigate()

  const list = useResource(() => get<DocumentRow[]>('/api/documents?limit=100'), {
    interval: 15_000,
  })

  return (
    <>
      <PageHeader
        title="Documents"
        lede="Everything it has read, and the fields it pulled out of each one. Nothing is recorded until you approve it."
      />

      <div className="stack">
        <UploadCard onQueued={() => void list.reload()} />

        <div className={`split${id ? ' has-detail' : ''}`}>
          <div className="panel split-list">
            <Async
              data={list.data}
              error={list.error}
              loading={list.loading}
              reload={list.reload}
              empty={
                <Empty title="No documents yet">
                  Send one to the assistant on WhatsApp, drop a PDF in the watched inbox folder,
                  or upload one above.
                </Empty>
              }
            >
              {(docs) =>
                docs.map((d) => (
                  <button
                    key={d.id}
                    className={`list-row${String(d.id) === id ? ' is-selected' : ''}`}
                    onClick={() => navigate(`/documents/${d.id}`)}
                  >
                    <div className="list-title truncate">{d.filename}</div>
                    <div className="list-meta">
                      <StatusPill status={d.status} />
                      {d.doc_type && <span>{titleCase(d.doc_type)}</span>}
                      {d.source && <span className="dimmer">via {d.source}</span>}
                      <span className="dimmer">{ago(d.created_at)}</span>
                    </div>
                  </button>
                ))
              }
            </Async>
          </div>

          <div className="split-detail">
            <Link className="btn btn-quiet btn-sm back-link" to="/documents">
              <ChevronLeft size={13} /> All documents
            </Link>
            {id ? (
              <Detail id={Number(id)} />
            ) : (
              <div className="panel">
                <Empty title="Pick a document">
                  What it understood the document to be, and every field it read off the page.
                </Empty>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  )
}

function UploadCard({ onQueued }: { onQueued: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [instruction, setInstruction] = useState('')
  const [note, setNote] = useState<string | null>(null)
  const act = useAction()

  async function send() {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setNote('Choose a file first.')
      return
    }
    setNote(null)
    const form = new FormData()
    form.append('file', file)
    if (instruction.trim()) form.append('instruction', instruction.trim())

    const ok = await act.run(async () => {
      const res = await upload<{ queued: number }>('/api/documents/upload', form)
      setNote(`Reading ${file.name}. The fields it finds will appear under Approvals.`)
      if (fileRef.current) fileRef.current.value = ''
      setInstruction('')
      onQueued()
      return res
    })
    if (!ok) setNote(null)
  }

  return (
    <Section title="Read a document">
      <div className="form-grid">
        <label className="field" style={{ display: 'block', marginBottom: 0 }}>
          <span className="field-label">File</span>
          <input className="input" type="file" ref={fileRef} accept={ACCEPT} />
        </label>
        <label className="field" style={{ display: 'block', marginBottom: 0, gridColumn: 'span 2' }}>
          <span className="field-label">What to do with it (optional)</span>
          <input
            className="input"
            placeholder="Extract the line items"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
          />
        </label>
        <FormAction>
          <button className="btn btn-primary" onClick={send} disabled={act.pending}>
            <Upload size={14} /> {act.pending ? 'Sending…' : 'Read it'}
          </button>
        </FormAction>
      </div>
      {act.error && <div className="banner banner-broken" style={{ marginTop: 'var(--s3)' }}>{act.error}</div>}
      {note && <div className="banner" style={{ marginTop: 'var(--s3)' }}>{note}</div>}
      <p className="dimmer" style={{ fontSize: 'var(--fs-tiny)', marginTop: 'var(--s3)' }}>
        PDF, JPG, PNG or WebP.
      </p>
    </Section>
  )
}

function Detail({ id }: { id: number }) {
  const { data, error, loading, reload } = useResource(
    () => get<DocumentDetail>(`/api/documents/${id}`),
    { interval: 20_000, deps: [id] },
  )

  return (
    <Async data={data} error={error} loading={loading} reload={reload}>
      {(d) => (
        <div className="stack">
          <section className="panel">
            <div className="panel-head">
              <div className="grow" style={{ minWidth: 0 }}>
                <h2 className="panel-title truncate">{d.filename}</h2>
                <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)', marginTop: 3 }}>
                  <span className="mono">#{d.id}</span> ·{' '}
                  <span title={isoTitle(d.created_at)}>{ago(d.created_at)}</span>
                </div>
              </div>
              <StatusPill status={d.status} />
            </div>
            <div className="panel-body stack-3">
              <KeyValue
                rows={[
                  ['Type', d.doc_type ? titleCase(d.doc_type) : '—'],
                  ['Arrived via', d.source || '—'],
                  [
                    'Run',
                    d.task_run_id ? (
                      <Link to={`/system/runs/${d.task_run_id}`}>#{d.task_run_id}</Link>
                    ) : (
                      '—'
                    ),
                  ],
                ]}
              />
              {d.summary && <p className="dim">{d.summary}</p>}
            </div>
          </section>

          {d.records.length === 0 ? (
            <Section title="Extracted fields">
              <Empty title="Nothing extracted yet">
                {d.status === 'failed'
                  ? 'It could not read this one. The run trace says why.'
                  : 'Still reading, or waiting for the approval that records it.'}
              </Empty>
            </Section>
          ) : (
            d.records.map((r) => {
              const rec = asObj<Record<string, unknown>>(r.record)
              const raw = (rec.fields ?? rec.data ?? []) as unknown
              const fields: ExtractedField[] = Array.isArray(raw)
                ? (raw as ExtractedField[])
                : raw && typeof raw === 'object'
                  ? Object.entries(raw as Record<string, unknown>).map(([name, value]) => ({ name, value }))
                  : []
              return (
                <Section
                  key={r.id}
                  title={String(rec.doc_type || 'Extracted record')}
                  meta={r.approved_by ? `approved by ${r.approved_by}` : 'not yet approved'}
                >
                  {fields.length ? (
                    <ExtractedTable fields={fields} />
                  ) : (
                    <pre className="code">{truncate(JSON.stringify(rec, null, 2), 4000)}</pre>
                  )}
                </Section>
              )
            })
          )}
        </div>
      )}
    </Async>
  )
}
