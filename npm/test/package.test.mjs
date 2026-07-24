import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const npmRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(npmRoot, "..");
const packageMetadata = JSON.parse(readFileSync(join(npmRoot, "package.json"), "utf8"));

test("npm wrapper stages the locked Python package and reports the same version", () => {
  const temporaryRoot = mkdtempSync(join(tmpdir(), "mermaid-ai-links-npm-"));
  const stagedProject = join(npmRoot, "python");
  try {
    execFileSync("node", [join(npmRoot, "scripts", "stage-python.mjs")], {
      cwd: repositoryRoot,
      stdio: "pipe",
    });
    const output = execFileSync("node", [join(npmRoot, "bin", "mermaid-ai-links.mjs"), "--version"], {
      cwd: repositoryRoot,
      encoding: "utf8",
      env: {
        ...process.env,
        MERMAID_AI_LINKS_NPM_VENV: join(temporaryRoot, ".venv"),
      },
    });

    assert.equal(output.trim(), `mermaid-ai-links ${packageMetadata.version}`);
    assert.match(readFileSync(join(stagedProject, "pyproject.toml"), "utf8"), /name = "mermaid-ai-links"/);
    assert.ok(readFileSync(join(stagedProject, "uv.lock"), "utf8").startsWith("version = 1"));
  } finally {
    execFileSync("node", [join(npmRoot, "scripts", "stage-python.mjs"), "--clean"], {
      cwd: repositoryRoot,
      stdio: "pipe",
    });
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});
