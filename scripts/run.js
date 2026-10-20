// Cross-platform launcher: finds Python, creates a venv, installs Flask, runs the app.
const { spawnSync, spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const isWin = process.platform === "win32";
const venvDir = path.join(root, "venv");
const venvPy = path.join(venvDir, isWin ? "Scripts" : "bin", isWin ? "python.exe" : "python");

function findPython() {
  for (const cmd of isWin ? ["python", "py"] : ["python3", "python"]) {
    const r = spawnSync(cmd, ["--version"], { encoding: "utf8" });
    if (r.status === 0) return cmd;
  }
  console.error("\nPython 3 was not found. Install it from https://www.python.org/downloads/\n(on Windows tick 'Add Python to PATH'), then run this again.");
  process.exit(1);
}

function run(cmd, args) {
  const r = spawnSync(cmd, args, { cwd: root, stdio: "inherit" });
  if (r.status !== 0) process.exit(r.status || 1);
}

function setup() {
  if (!fs.existsSync(venvPy)) {
    console.log("> Creating virtual environment (first run only)...");
    run(findPython(), ["-m", "venv", "venv"]);
  }
  const stamp = path.join(venvDir, ".deps-installed");
  const req = path.join(root, "requirements.txt");
  if (!fs.existsSync(stamp) || fs.statSync(req).mtimeMs > fs.statSync(stamp).mtimeMs) {
    console.log("> Installing Python dependencies...");
    run(venvPy, ["-m", "pip", "install", "-r", "requirements.txt"]);
    fs.writeFileSync(stamp, new Date().toISOString());
  }
}

const task = process.argv[2] || "dev";
setup();
if (task === "setup") { console.log("Setup complete. Now run: npm run dev"); process.exit(0); }

const args = { dev: ["app.py"], seed: ["app.py", "seed"], test: ["test_app.py"] }[task];
if (!args) { console.error("Unknown task: " + task); process.exit(1); }
if (task === "dev") console.log("> Starting Gatherly at http://localhost:" + (process.env.PORT || 5000) + "  (Ctrl+C to stop)");
const child = spawn(venvPy, args, { cwd: root, stdio: "inherit" });
child.on("exit", (code) => process.exit(code || 0));
process.on("SIGINT", () => child.kill("SIGINT"));
