import { appendFileSync, existsSync, readdirSync, readFileSync, statSync } from "node:fs"
import { join, relative } from "node:path"

export const ROOT = process.cwd()

export function metricsPath(): string {
  const path = process.env.UPHILL_METRICS
  if (!path) throw new Error("UPHILL_METRICS environment variable is not set")
  return path
}

export function quote(value: string): string {
  const escaped = value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r?\n/g, " ")
  return `"${escaped}"`
}

export function emit(
  metric: string,
  value: string | number,
  opts?: { item?: string; target?: string | number; note?: string }
): void {
  const parts = [metric]
  if (opts?.item !== undefined) parts.push(quote(opts.item))
  parts.push(String(value))
  if (opts?.target !== undefined) parts.push("target", String(opts.target))
  if (opts?.note !== undefined) parts.push("note", quote(opts.note))
  appendFileSync(metricsPath(), `${parts.join(" ")}\n`)
}

export function walkFiles(dir: string, exts: string | string[]): string[] {
  const extList = Array.isArray(exts) ? exts : [exts]
  const out: string[] = []
  if (!existsSync(dir)) return out
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith(".") || entry.name === "zig-out" || entry.name === "zig-pkg" || entry.name === "testdata") continue
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      out.push(...walkFiles(full, extList))
    } else if (extList.some((ext) => entry.name.endsWith(ext))) {
      out.push(relative(ROOT, full))
    }
  }
  return out.sort()
}

export function packageKey(relPath: string): string {
  const parts = relPath.split("/")
  if (parts[0] === "src" && parts.length >= 3) return `${parts[0]}/${parts[1]}/${parts[2]}`
  if (parts[0] === "bin" && parts.length >= 2) return `${parts[0]}/${parts[1]}`
  return relPath
}

export function groupByPackage(files: string[]): Map<string, string[]> {
  const map = new Map<string, string[]>()
  for (const file of files) {
    const key = packageKey(file)
    const list = map.get(key)
    if (list) list.push(file)
    else map.set(key, [file])
  }
  return new Map([...map.entries()].sort((a, b) => a[0].localeCompare(b[0])))
}

export interface ZigFileStats {
  totalLines: number
  prodLines: number
  bytes: number
  prodFnCount: number
  assertCount: number
  testCount: number
  hasFuzz: boolean
  pubDecls: number
  documentedPubDecls: number
  longLineHit?: { line: number; len: number }
  unprovedPanicHit?: { line: number; text: string }
  todoHit?: { line: number }
  flashblockHit?: { line: number }
}

function stripZigLine(line: string): string {
  let out = ""
  let inString = false
  let inChar = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    const next = line[i + 1]
    if (!inString && !inChar && ch === "/" && next === "/") break
    if (!inChar && ch === '"' && line[i - 1] !== "\\") {
      inString = !inString
      out += " "
      continue
    }
    if (!inString && ch === "'" && line[i - 1] !== "\\") {
      inChar = !inChar
      out += " "
      continue
    }
    out += inString || inChar ? " " : ch
  }
  return out
}

export function analyzeZigFile(relPath: string): ZigFileStats {
  const full = join(ROOT, relPath)
  const raw = readFileSync(full, "utf8")
  const bytes = statSync(full).size
  const lines = raw.split(/\r?\n/)

  let prodLines = 0
  let prodFnCount = 0
  let assertCount = 0
  let testCount = 0
  let hasFuzz = false
  let pubDecls = 0
  let documentedPubDecls = 0
  let longLineHit: { line: number; len: number } | undefined
  let unprovedPanicHit: { line: number; text: string } | undefined
  let todoHit: { line: number } | undefined
  let flashblockHit: { line: number } | undefined

  let inTest = false
  let testDepth = 0
  let braceDepth = 0

  for (let i = 0; i < lines.length; i++) {
    const lineNo = i + 1
    const text = lines[i]
    const trimmed = text.trim()
    const stripped = stripZigLine(text)

    // Line length <= 100 excluding multiline string literals (matches etc/scripts/tidy.sh)
    if (!longLineHit && text.length > 100 && !/^\s*\\\\/.test(text)) {
      longLineHit = { line: lineNo, len: text.length }
    }

    if (!hasFuzz && (/\bstd\.testing\.fuzz\b/.test(stripped) || /\bfuzz[A-Za-z0-9_]*\b/.test(trimmed))) {
      hasFuzz = true
    }

    if (!inTest && /^test\s+(".*"|\{)/.test(trimmed)) {
      testCount++
      inTest = true
      testDepth = 0
      for (const ch of stripped) {
        if (ch === "{") testDepth++
        else if (ch === "}") testDepth--
      }
      if (testDepth <= 0 && stripped.includes("}")) inTest = false
      continue
    }

    if (inTest) {
      for (const ch of stripped) {
        if (ch === "{") testDepth++
        else if (ch === "}") testDepth--
      }
      if (testDepth <= 0) inTest = false
      continue
    }

    prodLines++

    if (/^[ ]*(?:pub\s+)?(?:inline\s+)?fn\s+[A-Za-z0-9_]+\(/.test(stripped) && !/\)\s*type\s*\{/.test(stripped)) {
      prodFnCount++
    }

    const assertsOnLine = stripped.match(/\bassert\s*\(/g)
    if (assertsOnLine) assertCount += assertsOnLine.length

    if (braceDepth === 0 && /^pub\s+(fn|const)\s+[A-Za-z_]\w*/.test(trimmed)) {
      pubDecls++
      let k = i - 1
      while (k >= 0 && lines[k].trim() === "") k--
      if (k >= 0 && lines[k].trim().startsWith("///")) documentedPubDecls++
    }

    if (!unprovedPanicHit && /\B@panic\s*\(|\bcatch\s+unreachable\b/.test(stripped)) {
      const prevLine = i > 0 ? lines[i - 1] : ""
      const hasProofComment = /\/\//.test(text) || /^\s*\/\//.test(prevLine)
      if (!hasProofComment) {
        unprovedPanicHit = { line: lineNo, text: trimmed.slice(0, 80) }
      }
    }

    if (!todoHit && /\b(TODO|FIXME|XXX|HACK)\b/.test(text)) {
      todoHit = { line: lineNo }
    }

    if (!flashblockHit && /\bflashblock/i.test(stripped)) {
      flashblockHit = { line: lineNo }
    }

    for (const ch of stripped) {
      if (ch === "{") braceDepth++
      else if (ch === "}") braceDepth = Math.max(0, braceDepth - 1)
    }
  }

  return {
    totalLines: lines.length,
    prodLines,
    bytes,
    prodFnCount,
    assertCount,
    testCount,
    hasFuzz,
    pubDecls,
    documentedPubDecls,
    longLineHit,
    unprovedPanicHit,
    todoHit,
    flashblockHit
  }
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(2)}MB`
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(2)}kB`
  return `${bytes}B`
}
