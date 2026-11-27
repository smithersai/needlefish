import { join } from "node:path"
import { analyzeZigFile, emit, groupByPackage, ROOT, walkFiles } from "./_lib.ts"

const srcFiles = walkFiles(join(ROOT, "src"), ".zig")
const packages = groupByPackage(srcFiles)

for (const [pkg, files] of packages) {
  if (!pkg.startsWith("src/")) continue
  let testCount = 0
  let hasFuzz = false

  for (const file of files) {
    const stats = analyzeZigFile(file)
    testCount += stats.testCount
    if (stats.hasFuzz) hasFuzz = true
  }

  emit("unit-tests", testCount, { item: pkg })
  emit("fuzz-presence", hasFuzz ? "pass" : "fail", {
    item: pkg,
    note: hasFuzz ? undefined : "Add std.testing.fuzz or seeded Prng fuzz test for this package"
  })
}
