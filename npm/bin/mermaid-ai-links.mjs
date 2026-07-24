#!/usr/bin/env node

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const projectRoot = join(packageRoot, "python");
const packageMetadata = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8"));
const environmentRoot = process.env.MERMAID_AI_LINKS_NPM_VENV
  || join(homedir(), ".cache", "mermaid-ai-links", "npm", packageMetadata.version);
const uvExecutable = process.env.MERMAID_AI_LINKS_UV || "uv";
const uvArguments = [
  "run",
  "--project",
  projectRoot,
  "--frozen",
  "--no-editable",
  "mermaid-ai-links",
  ...process.argv.slice(2),
];
const childEnvironment = {
  ...process.env,
  UV_PROJECT_ENVIRONMENT: environmentRoot,
};
delete childEnvironment.VIRTUAL_ENV;

const child = spawn(uvExecutable, uvArguments, {
  stdio: "inherit",
  env: childEnvironment,
});

let forwardedSignal = null;
const signalExitCodes = {
  SIGHUP: 129,
  SIGINT: 130,
  SIGTERM: 143,
};
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => {
    forwardedSignal = signal;
    if (!child.killed) {
      child.kill(signal);
    }
  });
}

child.once("error", (error) => {
  if (error.code === "ENOENT") {
    console.error(
      "mermaid-ai-links 需要 uv。请先安装：https://docs.astral.sh/uv/getting-started/installation/",
    );
  } else {
    console.error(`无法启动 mermaid-ai-links：${error.message}`);
  }
  process.exitCode = 1;
});

child.once("exit", (code, signal) => {
  process.exitCode = code ?? signalExitCodes[signal || forwardedSignal] ?? 1;
});
