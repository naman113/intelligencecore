import { readModelChange, formatMatchRate, fmtNum } from '../../lib/model'
import { Sparkline } from '../Sparkline'
import { ErrorNote, KeyValue, StatusPill } from '../ui'

/* A proposed semantic-model change with the evidence the owner needs: the
 * definition, before and after, the compiled SQL, sample values and match rate.
 * Everything is agent- or database-written and renders as text nodes only. */

const cell = (v: unknown): string => {
  if (v == null) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

const json = (v: unknown): string => {
  try {
    return JSON.stringify(v, null, 2) ?? ''
  } catch {
    return ''
  }
}

export function ModelChangeDetail({ payload }: { payload: unknown }) {
  const c = readModelChange(payload)
  const defRows: [string, string][] = Object.entries(c.definition).map(([k, v]) => [k, cell(v)])
  const vals = c.samples.map((s) => s.value).filter((v): v is number => v != null)
  const small = { fontSize: 'var(--fs-tiny)' }

  return (
    <div className="stack-2">
      <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <StatusPill tone="neutral" label={(c.object ?? 'model').replace(/_/g, ' ')} />
        <span className="dim">{c.op ?? ''}</span>
        <span className="mono">{c.name ?? ''}</span>
      </div>

      {defRows.length > 0 && <KeyValue rows={defRows} />}

      {c.before && (
        <div className="stack-2">
          <div className="dimmer" style={small}>
            Before
          </div>
          <pre className="code dim">{json(c.before)}</pre>
          <div className="dimmer" style={small}>
            After
          </div>
          <pre className="code">{json(c.definition)}</pre>
        </div>
      )}

      {c.error && <ErrorNote message={c.error} />}

      {c.sql && (
        <div className="stack-2">
          <div className="dimmer" style={small}>
            Compiled SQL
          </div>
          <pre className="code">{c.sql}</pre>
        </div>
      )}

      {vals.length > 0 ? (
        <div className="stack-2">
          <div className="dimmer" style={small}>
            {vals.length} sample value{vals.length === 1 ? '' : 's'}
            {c.sampleRange ? ` · ${c.sampleRange.start} to ${c.sampleRange.end}` : ''}
          </div>
          <div className="row" style={{ gap: 12, alignItems: 'center' }}>
            <Sparkline values={vals} />
            <span className="mono">
              first {fmtNum(vals[0])} · latest {fmtNum(vals[vals.length - 1])}
            </span>
          </div>
        </div>
      ) : (
        c.sampleValue != null && (
          <div className="stack-2">
            <div className="dimmer" style={small}>
              Sample value
            </div>
            <span className="mono">{fmtNum(c.sampleValue)}</span>
          </div>
        )
      )}

      {c.object === 'relationship' && c.matchRate != null && (
        <div className="stack-2">
          <div>Match rate: {formatMatchRate(c.matchRate, c.matched, c.fromNonnull)}</div>
          {(c.duplicateKeys != null || c.nullKeys != null) && (
            <div className="dimmer" style={small}>
              {c.duplicateKeys != null ? `${fmtNum(c.duplicateKeys)} duplicate keys` : ''}
              {c.duplicateKeys != null && c.nullKeys != null ? ' · ' : ''}
              {c.nullKeys != null ? `${fmtNum(c.nullKeys)} null keys` : ''}
            </div>
          )}
        </div>
      )}

      {c.object === 'table' && (c.rowCount != null || c.note) && (
        <div className="stack-2">
          {c.rowCount != null && (
            <div>
              {fmtNum(c.rowCount)} rows
              {c.distinctKeys != null ? ` · ${fmtNum(c.distinctKeys)} distinct keys` : ''}
              {c.keyUnique != null ? (c.keyUnique ? ' · key is unique' : ' · key is NOT unique') : ''}
              {c.rowsPerDay != null ? ` · ${fmtNum(c.rowsPerDay)} rows per day` : ''}
            </div>
          )}
          {c.note && <div className="dim">{c.note}</div>}
        </div>
      )}

      {c.columns.length > 0 && (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                {c.columns.map((col, i) => (
                  <th key={`${col}-${i}`}>{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {c.previewRows.slice(0, 10).map((r, i) => (
                <tr key={i}>
                  {c.columns.map((col, j) => (
                    <td key={`${col}-${j}`} className="mono">
                      {cell(r[col])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
