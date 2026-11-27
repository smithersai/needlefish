import { spawnSync } from "node:child_process"
import { join } from "node:path"
import { analyzeZigFile, emit, groupByPackage, packageKey, ROOT, walkFiles } from "./_lib.ts"

const zigFiles = [
  ...walkFiles(join(ROOT, "src"), ".zig"),
  ...walkFiles(join(ROOT, "bin"), ".zig")
]
const packages = groupByPackage(zigFiles)

// 1. zig fmt --check across root files, src/, and bin/
const fmt = spawnSync("zig", ["fmt", "--check", "build.zig", "packages.zon", "src", "bin"], {
  cwd: ROOT,
  encoding: "utf8"
})
const unformattedByPkg = new Map<string, string>()
for (const line of `${fmt.stdout ?? ""}\n${fmt.stderr ?? ""}`.split(/\r?\n/)) {
  const trimmed = line.trim()
  if (!trimmed.endsWith(".zig") && !trimmed.endsWith(".zon")) continue
  const pkg = packageKey(trimmed)
  if (!unformattedByPkg.has(pkg)) unformattedByPkg.set(pkg, trimmed)
}

for (const [pkg] of packages) {
  const bad = unformattedByPkg.get(pkg)
  emit("zig-fmt", bad ? "fail" : "pass", {
    item: pkg,
    note: bad ? `Run \`zig fmt\` on ${bad}` : undefined
  })
}

// 2. Function length <= 70 lines via etc/scripts/fn-length.awk (TigerStyle)
const fnLen = spawnSync("awk", ["-v", "max=70", "-f", "etc/scripts/fn-length.awk", ...zigFiles], {
  cwd: ROOT,
  encoding: "utf8"
})
const longFnByPkg = new Map<string, string>()
for (const line of (fnLen.stdout ?? "").split(/\r?\n/)) {
  const trimmed = line.trim()
  if (!trimmed) continue
  const filePart = trimmed.split(":")[0]
  const pkg = packageKey(filePart)
  if (!longFnByPkg.has(pkg)) longFnByPkg.set(pkg, trimmed)
}

for (const [pkg, files] of packages) {
  const longFn = longFnByPkg.get(pkg)
  emit("fn-length", longFn ? "fail" : "pass", {
    item: pkg,
    note: longFn ? `${longFn} exceeds 70-line TigerStyle limit` : undefined
  })

  let longLineMsg: string | undefined
  let prodFns = 0
  let asserts = 0

  for (const file of files) {
    const stats = analyzeZigFile(file)
    if (!longLineMsg && stats.longLineHit) {
      longLineMsg = `${file}:${stats.longLineHit.line} (${stats.longLineHit.len} cols > 100)`
    }
    prodFns += stats.prodFnCount
    asserts += stats.assertCount
  }

  emit("line-length", longLineMsg ? "fail" : "pass", {
    item: pkg,
    note: longLineMsg
  })

  // TigerStyle asserts per 100 production functions (target 200 = >= 2.00 asserts/fn)
  if (prodFns > 0 && pkg.startsWith("src/")) {
    const per100 = Math.round((asserts * 100) / prodFns)
    emit("asserts-per-100-fns", per100, {
      item: pkg,
      note: per100 < 200 ? `${asserts} asserts across ${prodFns} fns (${(asserts / prodFns).toFixed(2)}/fn)` : undefined
    })
  }
}
