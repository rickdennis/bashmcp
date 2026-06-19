// fc-agent-runner — runs a Claude agent loop INSIDE the microVM via the Claude Agent SDK,
// emitting newline-delimited stream-json events on stdout. The host launches this through
// fc-agent's /exec_async path and drains the events by byte offset (reattachable across
// reconnect and VM pause/resume, exactly like any other async command).
//
// P1: one-shot mode  (--prompt "<task>") runs a single agent turn and exits.
// P2 will add persistent multi-turn mode (streaming-input async generator from an input file).
//
// Config: /etc/fc-agent-runner/agent.json  -> {model, system, allowedTools, cwd}
// Auth:   ANTHROPIC_API_KEY in the environment (sourced from /etc/fc-agent-runner/anthropic.env
//         by the /usr/local/bin/fc-agent-runner wrapper).
import { query } from "@anthropic-ai/claude-agent-sdk";
import { readFileSync } from "node:fs";

const CONFIG_PATH = process.env.FC_AGENT_CONFIG || "/etc/fc-agent-runner/agent.json";

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function loadConfig() {
  try {
    return JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  } catch {
    return {};
  }
}

function arg(name) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main() {
  const cfg = loadConfig();
  const prompt = arg("--prompt");
  if (prompt === undefined) {
    emit({ type: "error", error: "fc-agent-runner: --prompt is required (P1 one-shot mode)" });
    process.exit(2);
  }
  const options = {
    model: cfg.model || "claude-sonnet-4-6",
    permissionMode: "bypassPermissions", // headless: auto-approve tools, no TTY prompts
    allowedTools: cfg.allowedTools || ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    stderr: (data) => process.stderr.write(data), // surface the CLI subprocess's stderr
  };
  if (cfg.system) options.systemPrompt = cfg.system;
  if (cfg.cwd) options.cwd = cfg.cwd;

  for await (const message of query({ prompt, options })) {
    emit(message);
  }
}

main().catch((err) => {
  emit({
    type: "error",
    error: err && err.message ? err.message : String(err),
    exitCode: err ? (err.exitCode ?? err.code) : undefined,
    stderr: err && err.stderr ? String(err.stderr).slice(0, 6000) : undefined,
    stack: err && err.stack ? String(err.stack).slice(0, 1500) : undefined,
  });
  process.exit(1);
});
