import { spawnSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { analyzeZigFile, emit, groupByPackage, ROOT, walkFiles } from "./_lib.ts"

// 1. Package dependency graph & acyclicity check (etc/scripts/packages.py check)
const pkgCheck = spawnSync("python3", ["-I", "etc/scripts/packages.py", "check"], {
  cwd: ROOT,
  encoding: "utf8"
})
emit("package-graph", pkgCheck.status === 0 ? "pass" : "fail", {
  note: pkgCheck.status === 0 ? undefined : (pkgCheck.stderr || pkgCheck.stdout).trim().slice(0, 180)
})

// 2. CI shard coverage check (etc/scripts/ci-shards.py check)
const shardCheck = spawnSync("python3", ["-I", "etc/scripts/ci-shards.py", "check"], {
  cwd: ROOT,
  encoding: "utf8"
})
emit("ci-shards", shardCheck.status === 0 ? "pass" : "fail", {
  note: shardCheck.status === 0 ? undefined : (shardCheck.stderr || shardCheck.stdout).trim().slice(0, 180)
})

// 3. Zero external dependencies rule (AGENTS.md rule 4)
const zon = readFileSync(join(ROOT, "build.zig.zon"), "utf8")
const hasUrlDep = /^\s*\.[a-z_]+\s*=\s*\.\{\s*\.url/m.test(zon)
emit("zero-dependencies", hasUrlDep ? "fail" : "pass", {
  note: hasUrlDep ? "build.zig.zon must declare zero external dependencies" : undefined
})

// 4. Per-package production safety checks (unproved panic/unreachable, TODOs, no flashblocks)
const srcFiles = walkFiles(join(ROOT, "src"), ".zig")
const packages = groupByPackage(srcFiles)

for (const [pkg, files] of packages) {
  let panicNote: string | undefined
  let todoNote: string | undefined
  let flashNote: string | undefined

  for (const file of files) {
    const stats = analyzeZigFile(file)
    if (!panicNote && stats.unprovedPanicHit) {
      panicNote = `${file}:${stats.unprovedPanicHit.line}: @panic or catch unreachable without proof comment`
    }
    if (!todoNote && stats.todoHit) {
      todoNote = `${file}:${stats.todoHit.line}: unresolved TODO/FIXME marker`
    }
    if (!flashNote && stats.flashblockHit) {
      flashNote = `${file}:${stats.flashblockHit.line}: flashblock reference forbidden by AGENTS.md rule 10`
    }
  }

  emit("no-unproved-panic", panicNote ? "fail" : "pass", { item: pkg, note: panicNote })
  emit("no-todo", todoNote ? "fail" : "pass", { item: pkg, note: todoNote })
  emit("no-flashblocks", flashNote ? "fail" : "pass", { item: pkg, note: flashNote })
}
