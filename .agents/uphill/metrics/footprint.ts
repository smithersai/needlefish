import { readFileSync } from "node:fs"
import { join } from "node:path"
import { analyzeZigFile, emit, formatBytes, groupByPackage, ROOT, walkFiles } from "./_lib.ts"

const zigFiles = [
  ...walkFiles(join(ROOT, "src"), ".zig"),
  ...walkFiles(join(ROOT, "bin"), ".zig")
]
const packages = groupByPackage(zigFiles)

for (const [pkg, files] of packages) {
  let prodLines = 0
  let totalBytes = 0
  for (const file of files) {
    const stats = analyzeZigFile(file)
    prodLines += stats.prodLines
    totalBytes += stats.bytes
  }
  if (prodLines === 0) continue
  emit("package-prod-lines", prodLines, { item: pkg })
  emit("package-bytes", formatBytes(totalBytes), { item: pkg })
}

// Tripwire: external Zig dependencies in build.zig.zon (must stay 0 per AGENTS.md rule 4)
const zon = readFileSync(join(ROOT, "build.zig.zon"), "utf8")
const urlDeps = zon.match(/\.url\s*=/g)?.length ?? 0
emit("zig-dependencies", urlDeps)
