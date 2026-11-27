import { existsSync, readFileSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { analyzeZigFile, emit, groupByPackage, ROOT, walkFiles } from "./_lib.ts"

const srcFiles = walkFiles(join(ROOT, "src"), ".zig")
const packages = groupByPackage(srcFiles)

for (const [pkg, files] of packages) {
  let pubDecls = 0
  let documented = 0
  for (const file of files) {
    const stats = analyzeZigFile(file)
    pubDecls += stats.pubDecls
    documented += stats.documentedPubDecls
  }
  if (pubDecls === 0) continue
  const pct = Math.round((documented * 100) / pubDecls)
  emit("pub-doc-coverage", pct, {
    item: pkg,
    note: pct < 80 ? `${documented}/${pubDecls} pub decls have /// doc comments` : undefined
  })
}

const mdFiles = [
  ...["README.md", "AGENTS.md"].filter((f) => existsSync(join(ROOT, f))),
  ...walkFiles(join(ROOT, "docs"), ".md")
]
for (const md of mdFiles) {
  const content = readFileSync(join(ROOT, md), "utf8")
  const links = [...content.matchAll(/\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)/g)]
  let broken: string | undefined
  for (const match of links) {
    const target = match[1].trim()
    if (!target || /^(https?:|mailto:|#)/.test(target)) continue
    const resolved = resolve(dirname(join(ROOT, md)), target)
    if (!existsSync(resolved)) {
      broken = `broken link target: ${target}`
      break
    }
  }
  emit("markdown-links", broken ? "fail" : "pass", { item: md, note: broken })
}
