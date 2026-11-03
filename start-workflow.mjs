import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const children = [];
let stopping = false;

function start(command, args, cwd = root) {
  const child = spawn(command, args, {
    cwd,
    stdio: "inherit",
    env: process.env,
  });
  children.push(child);
  child.on("error", (err) => {
    console.error(err.message);
    shutdown(1);
  });
  child.on("exit", (code, signal) => {
    if (stopping) return;
    if (signal) return;
    shutdown(code || 0);
  });
  return child;
}

function shutdown(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (child.exitCode !== null || child.signalCode) continue;
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { stdio: "ignore" });
    } else {
      child.kill("SIGTERM");
    }
  }
  setTimeout(() => process.exit(code), 400);
}

function deepseekReady() {
  return new Promise((resolve) => {
    const req = http.get("http://127.0.0.1:8000/healthz", (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.setTimeout(800, () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));

const deepseekDir = path.join(root, "Deepseek-API");
const deepseekPython = path.join(
  deepseekDir,
  "venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);

if (await deepseekReady()) {
  console.log("DeepSeek đã chạy tại http://127.0.0.1:8000");
} else if (!fs.existsSync(deepseekPython)) {
  console.error("Chưa có Deepseek-API\\venv. Trong thư mục Deepseek-API chạy: python -m venv venv rồi pip install -r requirements.txt");
  process.exit(1);
} else {
  start(deepseekPython, ["app.py"], deepseekDir);
}

start("python", ["-m", "pipeline.server"]);
start(process.execPath, [path.join(root, "node_modules", "vite", "bin", "vite.js")]);
