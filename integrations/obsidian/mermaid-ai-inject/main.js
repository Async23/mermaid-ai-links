const { Notice, Plugin, PluginSettingTab, Setting } = require("obsidian");
const { spawn } = require("child_process");
const os = require("os");
const path = require("path");

const DEFAULT_SETTINGS = {
  cliPath: path.join(os.homedir(), ".local", "bin", "inject-mermaid-ai"),
};

module.exports = class MermaidAIInjectPlugin extends Plugin {
  async onload() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    this.addSettingTab(new MermaidAIInjectSettingTab(this.app, this));
    this.addCommand({
      id: "open-current-mermaid-in-mermaid-ai",
      name: "Open current Mermaid in Mermaid.ai",
      editorCallback: (editor, view) => {
        void this.injectCurrentBlock(editor, view);
      },
    });
  }

  async injectCurrentBlock(editor, view) {
    try {
      const adapter = this.app.vault.adapter;
      if (typeof adapter.getBasePath !== "function") {
        throw new Error("只支持 Obsidian Desktop 的本地 Vault");
      }
      if (!view.file) {
        throw new Error("没有活动的 Markdown 文件");
      }

      const absolutePath = path.join(adapter.getBasePath(), view.file.path);
      const line = editor.getCursor().line + 1;
      new Notice("正在注入 Mermaid.ai…", 2000);
      const output = await runCli(this.settings.cliPath, [
        "--file",
        absolutePath,
        "--line",
        String(line),
      ]);
      const evidence = output.split("\n").filter(Boolean).at(-1) || "预览已更新";
      new Notice(`Mermaid.ai 注入成功\n${evidence}`, 5000);
    } catch (error) {
      console.error("Mermaid.ai Inject", error);
      new Notice(`Mermaid.ai 注入失败\n${error.message || error}`, 10000);
    }
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }
};

class MermaidAIInjectSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    this.containerEl.empty();
    new Setting(this.containerEl)
      .setName("inject-mermaid-ai path")
      .setDesc("CLI 的绝对路径；默认 ~/.local/bin/inject-mermaid-ai")
      .addText((text) => text
        .setPlaceholder(DEFAULT_SETTINGS.cliPath)
        .setValue(this.plugin.settings.cliPath)
        .onChange(async (value) => {
          this.plugin.settings.cliPath = value.trim() || DEFAULT_SETTINGS.cliPath;
          await this.plugin.saveSettings();
        }));
  }
}

function runCli(executable, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, {
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.once("error", (error) => {
      settled = true;
      reject(new Error(`无法启动 ${executable}: ${error.message}`));
    });
    child.once("close", (code) => {
      if (settled) return;
      if (code === 0) {
        resolve(stdout.trim());
      } else {
        reject(new Error((stderr || stdout || `inject-mermaid-ai exited ${code}`).trim()));
      }
    });
  });
}
