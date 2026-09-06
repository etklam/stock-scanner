// E2E serve harness: fresh temp data dir -> qscan init -> qscan demo (fixture,
// SYNTHETIC) -> qscan serve with the compiled UI. Writes the bearer token and
// URLs to e2e/.state.json (gitignored, synthetic token only) for the spec;
// kills serve on exit.
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..", "..");
const PORT = Number(process.env.QSCAN_E2E_PORT ?? 8944);
const DATA = join(tmpdir(), `qscan-e2e-${process.pid}-${Date.now()}`);
const QSCAN = ["uv", "run", "qscan"];

function run(args) {
  const result = spawnSync(QSCAN[0], [...QSCAN.slice(1), ...args], {
    cwd: ROOT,
    encoding: "utf-8",
    env: process.env,
  });
  if (result.status !== 0) {
    console.error(`qscan ${args.join(" ")} failed:`, result.stderr);
    process.exit(1);
  }
  return result.stdout;
}

mkdirSync(DATA, { recursive: true });
run(["--data-dir", DATA, "init"]);
const demo = JSON.parse(run(["--data-dir", DATA, "demo"]));

const serve = spawn(
  QSCAN[0],
  [
    ...QSCAN.slice(1),
    "--data-dir",
    DATA,
    "--provider",
    "fixture",
    "serve",
    "--port",
    String(PORT),
  ],
  { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"] },
);
serve.stderr.on("data", (chunk) => process.stderr.write(`[serve] ${chunk}`));

const deadline = Date.now() + 60_000;
let ready = false;
while (Date.now() < deadline) {
  try {
    const response = await fetch(`http://127.0.0.1:${PORT}/health/live`);
    if (response.ok) {
      ready = true;
      break;
    }
  } catch {
    // not up yet
  }
  await new Promise((r) => setTimeout(r, 200));
}
if (!ready) {
  console.error("e2e serve never became reachable");
  process.exit(1);
}

writeFileSync(
  join(import.meta.dirname, ".state.json"),
  JSON.stringify({
    dataDir: DATA,
    demo,
    port: PORT,
    token: JSON.parse(readFileSync(join(DATA, "api-token.json"), "utf-8")).token,
  }),
);
console.log(`e2e serve ready on http://127.0.0.1:${PORT}/ui/ (data ${DATA})`);

function shutdown() {
  serve.kill("SIGINT");
  setTimeout(() => {
    try {
      rmSync(DATA, { recursive: true, force: true });
    } catch {
      // best effort
    }
    process.exit(0);
  }, 1_000);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
