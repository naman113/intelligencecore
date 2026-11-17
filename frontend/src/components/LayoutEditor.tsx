import {
  PRESET_LABEL,
  TYPE_LABEL,
  WIDTHS,
  WIDTH_LABEL,
  canMoveDown,
  canMoveUp,
  moveDown,
  moveToSection,
  moveUp,
  offeredRanges,
  setDefaultRange,
  setHidden,
  setSectionTitle,
  setTitle,
  setWidth,
  type LTile,
  type LayoutState,
  type MoveResult,
  type Width,
} from '../lib/layout'
import './layout-editor.css'

/* Edit layout mode (UI-SPEC section 15). Presentational: the container owns the state.
 * Layout only; nothing about queries, measures, tile types or formats is editable.
 * The same controls render at every width; there is no pointer-drag. */

export interface LayoutEditorProps {
  state: LayoutState
  /** Called with the next state; `notice` carries the reason a move was refused. */
  onChange: (next: LayoutState, notice?: string | null) => void
  onSave: () => void
  onCancel: () => void
  busy?: boolean
  error?: string | null
  notice?: string | null
  /** False while nothing differs from the published version. */
  changed?: boolean
}

function sectionLabel(title: string, index: number): string {
  return title.trim() ? title : `Section ${index + 1}`
}

function Card({
  tile,
  state,
  onChange,
  busy,
}: {
  tile: LTile
  state: LayoutState
  onChange: LayoutEditorProps['onChange']
  busy?: boolean
}) {
  const apply = (r: MoveResult) => onChange(r.state, r.reason)
  const at = state.sections.findIndex((s) => s.tiles.some((t) => t.id === tile.id))
  return (
    <div className={`lt-card lt-w-${tile.width}${tile.hidden ? ' lt-hidden' : ''}`} data-tile={tile.id}>
      <div className="lt-card-head">
        <span className="lt-type">{TYPE_LABEL[tile.type] ?? tile.type}</span>
        {tile.hidden && <span className="lt-flag">Hidden</span>}
        <span className="lt-moves">
          <button
            type="button"
            className="btn btn-icon btn-sm"
            aria-label="Move up"
            title="Move up"
            disabled={busy || !canMoveUp(state, tile.id)}
            onClick={() => apply(moveUp(state, tile.id))}
          >
            {'↑'}
          </button>
          <button
            type="button"
            className="btn btn-icon btn-sm"
            aria-label="Move down"
            title="Move down"
            disabled={busy || !canMoveDown(state, tile.id)}
            onClick={() => apply(moveDown(state, tile.id))}
          >
            {'↓'}
          </button>
        </span>
      </div>
      <input
        className="input lt-title"
        type="text"
        maxLength={120}
        aria-label={`Title of ${tile.id}`}
        value={tile.title}
        placeholder={tile.type === 'text' ? 'No title' : 'Title'}
        disabled={busy}
        onChange={(e) => onChange(setTitle(state, tile.id, e.target.value))}
      />
      <div className="lt-row">
        <label className="lt-field">
          <span className="field-label">Width</span>
          <select
            className="select"
            value={tile.width}
            disabled={busy}
            onChange={(e) => onChange(setWidth(state, tile.id, e.target.value as Width))}
          >
            {WIDTHS.map((w) => (
              <option key={w} value={w}>
                {WIDTH_LABEL[w]}
              </option>
            ))}
          </select>
        </label>
        <label className="lt-field">
          <span className="field-label">Move to section</span>
          <select
            className="select"
            value={at}
            disabled={busy || state.sections.length < 2}
            onChange={(e) => {
              const r = moveToSection(state, tile.id, Number(e.target.value))
              onChange(r.state, r.reason)
            }}
          >
            {state.sections.map((s, i) => (
              <option key={i} value={i}>
                {sectionLabel(s.title, i)}
              </option>
            ))}
          </select>
        </label>
        <label className="lt-show">
          <input
            type="checkbox"
            checked={!tile.hidden}
            disabled={busy}
            onChange={(e) => onChange(setHidden(state, tile.id, !e.target.checked))}
          />{' '}
          Show
        </label>
      </div>
    </div>
  )
}

export function LayoutEditor(p: LayoutEditorProps) {
  const { state } = p
  return (
    <div className="lt-editor card">
      <h3 className="panel-title">Edit layout</h3>
      {p.error && <div className="banner banner-broken">{p.error}</div>}
      {p.notice && <p className="ic-meta">{p.notice}</p>}
      {state.hasDateControl && (
        <label className="lt-field lt-range">
          <span className="field-label">Default period</span>
          <select
            className="select"
            value={state.defaultRange ?? ''}
            disabled={p.busy}
            onChange={(e) => p.onChange(setDefaultRange(state, e.target.value || null))}
          >
            {offeredRanges(state).map((r) => (
              <option key={r} value={r}>
                {PRESET_LABEL[r] ?? r}
              </option>
            ))}
          </select>
        </label>
      )}
      {state.sections.map((sec, i) => (
        <div className="lt-section" key={i}>
          <input
            className="input lt-section-title"
            type="text"
            maxLength={60}
            aria-label={`Title of section ${i + 1}`}
            value={sec.title}
            placeholder={`Section ${i + 1}`}
            disabled={p.busy}
            onChange={(e) => p.onChange(setSectionTitle(state, i, e.target.value))}
          />
          <div className="lt-grid">
            {sec.tiles.map((t) => (
              <Card key={t.id} tile={t} state={state} onChange={p.onChange} busy={p.busy} />
            ))}
          </div>
        </div>
      ))}
      <div className="btn-row">
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={p.busy || p.changed === false}
          onClick={p.onSave}
        >
          Save as draft
        </button>
        <button type="button" className="btn btn-quiet btn-sm" disabled={p.busy} onClick={p.onCancel}>
          Cancel
        </button>
      </div>
      <p className="ic-meta">Layout only. Ask the agent to change numbers or charts.</p>
    </div>
  )
}
