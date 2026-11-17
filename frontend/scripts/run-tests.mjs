// Zero-new-dependency test runner. Planner decision: no vitest. Tests are bundled
// (output goes to .ic-tests: node --test skips anything under node_modules)
// with the esbuild that package-lock.json already pins (Vite's dependency) and run
// with Node's built-in node:test. Presentational components are tested through
// react-dom/server renderToStaticMarkup.
import { build } from 'esbuild'
import { spawnSync } from 'node:child_process'
import { existsSync, readdirSync, rmSync, statSync } from 'node:fs'
import { dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
process.chdir(root)

function collect(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...collect(p))
    else if (/\.test\.tsx?$/.test(name)) out.push(p)
  }
  return out
}

const args = process.argv.slice(2)
const files = args.length ? args : collect('tests')
const outdir = '.ic-tests'
rmSync(outdir, { recursive: true, force: true })

if (!files.length) {
  console.error('no test files found')
  process.exit(1)
}

try {
  await build({
    entryPoints: files,
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node20',
    jsx: 'automatic',
    packages: 'external',
    outdir,
    outbase: 'tests',
    outExtension: { '.js': '.mjs' },
    loader: { '.css': 'empty', '.png': 'empty', '.svg': 'empty' },
    sourcemap: 'inline',
    logLevel: 'warning',
  })
} catch {
  process.exit(1)
}

const bundled = files
  .map((f) => outdir + '/' + f.split(sep).join('/').replace(/^tests\//, '').replace(/\.tsx?$/, '.mjs'))
  .filter((f) => existsSync(f))
const r = spawnSync(
  process.execPath,
  ['--test', '--import', pathToFileURL(resolve('tests/setup.mjs')).href, ...bundled],
  { stdio: 'inherit' },
)
process.exit(r.status ?? 1)
