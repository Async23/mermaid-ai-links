#!/usr/bin/env node

import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const npmRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(npmRoot, "..");
const targetFlag = process.argv.indexOf("--target");
const targetRoot = targetFlag >= 0
  ? resolve(process.argv[targetFlag + 1])
  : join(npmRoot, "python");

if (process.argv.includes("--clean")) {
  await rm(targetRoot, { recursive: true, force: true });
  process.exit(0);
}

await rm(targetRoot, { recursive: true, force: true });
await mkdir(targetRoot, { recursive: true });
await Promise.all([
  cp(join(repositoryRoot, "src"), join(targetRoot, "src"), {
    recursive: true,
    filter: (source) => !source.includes("__pycache__") && !source.endsWith(".pyc"),
  }),
  cp(join(repositoryRoot, "pyproject.toml"), join(targetRoot, "pyproject.toml")),
  cp(join(repositoryRoot, "uv.lock"), join(targetRoot, "uv.lock")),
]);

const repositoryReadme = await readFile(join(repositoryRoot, "README.md"), "utf8");
await writeFile(join(targetRoot, "README.md"), repositoryReadme);
