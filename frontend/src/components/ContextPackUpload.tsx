import { useRef, useState } from 'react'
import { get, patch, upload } from '../lib/api'
import { getOperator, mainChat } from '../lib/ask'
import { buildPackForm, PACK_ACCEPT, validatePack } from '../lib/contextpack'
import { useAction, useResource } from '../lib/hooks'
import { useAsk } from './AskPanel'

/* Uploads a context pack to the agent's document path on the owner's main Ask chat
 * (Quick 003: pack watches alert there, as before multiple chats).
 * The UI never parses the pack; the context specialist proposes, the owner approves. */

export type SkillState = { enabled: boolean } | null

export function ContextPackUploadView({
  skill,
  note,
  error,
  busy,
  fileRef,
  onUpload,
  onEnable,
}: {
  skill: SkillState
  note?: string | null
  error?: string | null
  busy?: boolean
  fileRef?: React.RefObject<HTMLInputElement>
  onUpload?: () => void
  onEnable?: () => void
}) {
  return (
    <section className="panel" style={{ marginBottom: 'var(--s3)' }}>
      <div className="panel-body">
        <h2 className="panel-title">Import a context pack</h2>
        <p className="ic-meta">
          The import runs in your main Ask chat. Nothing is applied until you approve each proposal.
        </p>
        <div className="row" style={{ gap: 'var(--s2)', flexWrap: 'wrap' }}>
          <input className="input" type="file" ref={fileRef} accept={PACK_ACCEPT} />
          <button className="btn btn-sm" disabled={busy} onClick={onUpload}>
            Upload
          </button>
        </div>
        {skill && skill.enabled && <p className="ic-meta">context-pack-import skill: enabled</p>}
        {skill && !skill.enabled && (
          <p className="ic-meta">
            context-pack-import skill: disabled.{' '}
            <button className="btn btn-sm" onClick={onEnable}>
              Enable skill
            </button>
          </p>
        )}
        {note && <div className="banner">{note}</div>}
        {error && <div className="banner banner-broken">{error}</div>}
      </div>
    </section>
  )
}

export function ContextPackUpload() {
  const { openChat } = useAsk()
  const fileRef = useRef<HTMLInputElement>(null)
  const [note, setNote] = useState<string | null>(null)
  const act = useAction()
  const skill = useResource<SkillState>(async () => {
    try {
      return await get<{ enabled: boolean }>('/api/skills/context-pack-import')
    } catch {
      return null
    }
  })

  const send = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setNote('Choose a file first.')
      return
    }
    const bad = validatePack(file)
    if (bad) {
      setNote(bad)
      return
    }
    setNote(null)
    await act.run(async () => {
      const res = await upload<{ queued: number; filename: string; thread_id?: string }>(
        '/api/documents/upload',
        buildPackForm(file, file.name, getOperator()),
      )
      setNote(
        `Import started in your main Ask chat (task #${res.queued}). Proposals will appear here and on Approvals. Nothing is applied until you approve.`,
      )
      if (fileRef.current) fileRef.current.value = ''
      openChat(res.thread_id || mainChat())
    })
  }

  const enable = async () => {
    await act.run(async () => {
      await patch('/api/skills/context-pack-import', { enabled: true })
      await skill.reload()
    })
  }

  return (
    <ContextPackUploadView
      skill={skill.data ?? null}
      note={note}
      error={act.error}
      busy={act.pending}
      fileRef={fileRef}
      onUpload={() => void send()}
      onEnable={() => void enable()}
    />
  )
}
