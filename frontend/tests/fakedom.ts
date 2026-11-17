// Phase 22 (22-07): a minimal fake browser for the DOM half of ic-render-2.0.0.
// render.js is run in a node:vm context that has a fake document, fetch, Chart,
// IntersectionObserver, matchMedia and performance. Nothing here is a real layout
// engine: it records what the renderer built and which requests it made.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import vm from 'node:vm'

const SRC = readFileSync(
  join(process.cwd(), '..', 'dashboards', 'vendor', 'ic-render-2.0.0', 'render.js'),
  'utf8',
)

export class FakeEl {
  tag: string
  children: FakeEl[] = []
  parent: FakeEl | null = null
  dataset: Record<string, string> = {}
  style: Record<string, string> = {}
  listeners: Record<string, Array<(e: any) => void>> = {}
  role?: string
  ariaLabel?: string
  ariaExpanded?: string
  ariaPressed?: string
  ariaModal?: string
  ariaHasPopup?: string
  hidden = false
  value = ''
  type = ''
  min = ''
  max = ''
  disabled = false
  private _text = ''
  private _class = ''

  constructor(tag: string) {
    this.tag = tag
  }

  get className(): string {
    return this._class
  }
  set className(v: string) {
    this._class = String(v)
  }
  get classList() {
    const self = this
    const toks = () => self._class.split(/\s+/).filter(Boolean)
    return {
      add(c: string) {
        if (!toks().includes(c)) self._class = [...toks(), c].join(' ')
      },
      remove(c: string) {
        self._class = toks().filter((t) => t !== c).join(' ')
      },
      contains(c: string) {
        return toks().includes(c)
      },
      toggle(c: string) {
        if (toks().includes(c)) this.remove(c)
        else this.add(c)
      },
    }
  }
  get textContent(): string {
    return this._text + this.children.map((c) => c.textContent).join('')
  }
  set textContent(v: string) {
    this.children.forEach((c) => (c.parent = null))
    this.children = []
    this._text = String(v)
  }
  get firstChild(): FakeEl | null {
    return this.children.length ? this.children[0] : null
  }
  appendChild(c: FakeEl) {
    if (c.parent) c.parent.removeChild(c)
    c.parent = this
    this.children.push(c)
    return c
  }
  removeChild(c: FakeEl) {
    const i = this.children.indexOf(c)
    if (i < 0) throw new Error('not a child')
    this.children.splice(i, 1)
    c.parent = null
    return c
  }
  replaceChildren(...nodes: FakeEl[]) {
    this.children.forEach((c) => (c.parent = null))
    this.children = []
    nodes.forEach((n) => this.appendChild(n))
  }
  addEventListener(type: string, fn: (e: any) => void) {
    ;(this.listeners[type] ||= []).push(fn)
  }
  dispatch(type: string, evt: any = {}) {
    for (const fn of this.listeners[type] || []) fn({ type, target: this, ...evt })
  }
  click() {
    this.dispatch('click')
  }
}

export function walk(root: FakeEl, fn: (e: FakeEl) => void) {
  fn(root)
  root.children.forEach((c) => walk(c, fn))
}

export function findAll(root: FakeEl, cls: string): FakeEl[] {
  const out: FakeEl[] = []
  walk(root, (e) => {
    if (e.classList.contains(cls)) out.push(e)
  })
  return out
}

export function findTag(root: FakeEl, tag: string): FakeEl[] {
  const out: FakeEl[] = []
  walk(root, (e) => {
    if (e.tag === tag) out.push(e)
  })
  return out
}

export function text(e: FakeEl | undefined | null): string {
  return e ? e.textContent : ''
}

export function tile(root: FakeEl, id: string): FakeEl {
  const hit = findAll(root, 'ic-tile').find((t) => t.dataset.icTile === id)
  if (!hit) throw new Error('no tile ' + id)
  return hit
}

export interface EnvOpts {
  spec: any
  data?: Record<string, any>
  nowMs?: number
  hash?: string
  width?: number
  scheme?: 'light' | 'dark'
  specStatus?: number
  failData?: string[]
  noObserver?: boolean
}

export function makeEnv(o: EnvOpts) {
  const root = new FakeEl('div')
  const fetches: string[] = []
  const charts: any[] = []
  const marks: string[] = []
  const timers: Array<{ fn: unknown; ms: number }> = []
  const posted: any[] = []
  const observers: any[] = []
  const mqs: any[] = []
  const state = { width: o.width ?? 1200, scheme: o.scheme ?? 'dark' }

  class FakeChart {
    static defaults: any = { font: {} }
    canvas: any
    config: any
    destroyed = false
    hits: any[] = []
    constructor(canvas: any, config: any) {
      this.canvas = canvas
      this.config = config
      charts.push(this)
    }
    destroy() {
      this.destroyed = true
    }
    update() {}
    getElementsAtEventForMode() {
      return this.hits
    }
  }

  class FakeIO {
    cb: (entries: any[]) => void
    opts: any
    els: FakeEl[] = []
    constructor(cb: (entries: any[]) => void, opts: any) {
      this.cb = cb
      this.opts = opts
      observers.push(this)
    }
    observe(e: FakeEl) {
      this.els.push(e)
    }
    unobserve(e: FakeEl) {
      this.els = this.els.filter((x) => x !== e)
    }
    disconnect() {
      this.els = []
    }
  }

  function matches(q: string): boolean {
    const light = /prefers-color-scheme:\s*light/.exec(q)
    if (light) return state.scheme === 'light'
    const mx = /max-width:\s*(\d+)px/.exec(q)
    if (mx) return state.width <= +mx[1]
    const mn = /min-width:\s*(\d+)px/.exec(q)
    if (mn) return state.width >= +mn[1]
    return false
  }

  const document = {
    readyState: 'complete',
    title: '',
    createElement: (tag: string) => new FakeEl(tag),
    getElementById: (id: string) => (id === 'ic-root' ? root : null),
    addEventListener() {},
  }
  const location = { search: '?t=x', hash: o.hash ?? '' }

  const fetchFn = (url: string) => {
    fetches.push(url)
    const done = (status: number, body: any) =>
      Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) })
    if (url === './spec?t=x') return o.specStatus ? done(o.specStatus, {}) : done(200, o.spec)
    const m = /^\.\/data\/([a-z0-9_]+)\?t=x$/.exec(url)
    if (m && o.data && m[1] in o.data && !(o.failData || []).includes(m[1])) return done(200, o.data[m[1]])
    return done(404, {})
  }

  const sandbox: any = {
    document,
    location,
    fetch: fetchFn,
    Chart: FakeChart,
    matchMedia: (q: string) => {
      const mq: any = {
        media: q,
        listeners: [] as Array<() => void>,
        get matches() {
          return matches(q)
        },
        addEventListener(_t: string, fn: () => void) {
          mq.listeners.push(fn)
        },
      }
      mqs.push(mq)
      return mq
    },
    performance: { mark: (n: string) => marks.push(n) },
    setTimeout: (fn: unknown, ms: number) => {
      timers.push({ fn, ms })
      return timers.length
    },
    parent: { postMessage: (m: any, t: any) => posted.push([m, t]) },
    __nowMs: o.nowMs ?? Date.UTC(2026, 10, 4, 10, 0, 0),
    Intl,
  }
  if (!o.noObserver) sandbox.IntersectionObserver = FakeIO
  const context = vm.createContext(sandbox)
  vm.runInContext('Date.now = function () { return __nowMs; }', context)
  vm.runInContext(SRC, context)

  async function flush() {
    for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r))
  }

  return {
    context,
    root,
    charts,
    marks,
    observers,
    fetches,
    timers,
    posted,
    flush,
    location,
    document,
    setNow(ms: number) {
      sandbox.__nowMs = ms
    },
    setScheme(s: 'light' | 'dark') {
      state.scheme = s
      mqs.filter((m) => /color-scheme/.test(m.media)).forEach((m) => m.listeners.forEach((f: () => void) => f()))
    },
    setWidth(w: number) {
      state.width = w
      mqs.filter((m) => /width/.test(m.media)).forEach((m) => m.listeners.forEach((f: () => void) => f()))
    },
    // near observer is created first, far observer second
    near: () => observers[0],
    far: () => observers[1],
    // report every tile as near the viewport
    showAll() {
      const obs = observers[0]
      obs.cb(obs.els.map((e: FakeEl) => ({ target: e, isIntersecting: true })))
    },
    intersect(which: 0 | 1, el: FakeEl, isIntersecting: boolean) {
      observers[which].cb([{ target: el, isIntersecting }])
    },
    liveCharts: () => charts.filter((c) => !c.destroyed),
  }
}

export type Env = ReturnType<typeof makeEnv>
