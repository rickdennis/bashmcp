// fc-agent-runner — runs a Claude agent loop INSIDE the microVM via the Claude Agent SDK,
// emitting newline-delimited stream-json events on stdout. The host launches this through
// fc-agent's /exec_async path and drains events by byte offset (reattachable across
// reconnect and VM pause/resume, like any other async command).
//
//   --prompt "<task>"   one-shot: run a single turn and exit.
//   --session <id>      persistent: keep one query() loop alive, taking each newline-JSON
//                       user message appended to /var/run/fc-agent-runner/<id>.in as the
//                       next turn (conversation state retained). A {"type":"stop"} line ends it.
//
// Config: /etc/fc-agent-runner/agent.json -> {model, system, allowedTools, cwd}
// Auth:   ANTHROPIC_API_KEY in the env (the /usr/local/bin/fc-agent-runner wrapper exports it).
import { query } from "@anthropic-ai/claude-agent-sdk";
import { readFileSync, existsSync } from "node:fs";
import { open, mkdir, writeFile } from "node:fs/promises";

const CONFIG_PATH = process.env.FC_AGENT_CONFIG || "/etc/fc-agent-runner/agent.json";
const INPUT_DIR = "/var/run/fc-agent-runner";

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

function buildOptions(cfg) {
  const o = {
    model: cfg.model || "claude-sonnet-4-6",
    permissionMode: "bypassPermissions", // headless: auto-approve tools (microVM is the sandbox)
    allowedTools: cfg.allowedTools || ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    stderr: (data) => process.stderr.write(data), // surface the CLI subprocess's stderr
  };
  if (cfg.system) o.systemPrompt = cfg.system;
  if (cfg.cwd) o.cwd = cfg.cwd;
  return o;
}

// Tail newline-JSON user messages from inputPath, yielding SDK user messages. Polls for
// appends so the SDK query() loop stays alive (idle) between turns. {"type":"stop"} ends it.
async function* userMessages(inputPath) {
  let pos = 0;
  let buf = "";
  for (;;) {
    try {
      const fh = await open(inputPath, "r");
      const st = await fh.stat();
      if (st.size > pos) {
        const len = st.size - pos;
        const b = Buffer.alloc(len);
        const { bytesRead } = await fh.read(b, 0, len, pos);
        buf += b.subarray(0, bytesRead).toString("utf8");
        pos += bytesRead;
      }
      await fh.close();
    } catch {
      /* file not created yet */
    }
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        continue;
      }
      if (obj.type === "stop") return;
      const content = typeof obj.content === "string" ? obj.content : (obj.message?.content ?? "");
      yield { type: "user", message: { role: "user", content } };
    }
    await new Promise((r) => setTimeout(r, 200));
  }
}

async function main() {
  const cfg = loadConfig();
  const options = buildOptions(cfg);

  const oneShot = arg("--prompt");
  if (oneShot !== undefined) {
    for await (const m of query({ prompt: oneShot, options })) emit(m);
    return;
  }

  const sessionId = arg("--session");
  if (sessionId) {
    await mkdir(INPUT_DIR, { recursive: true });
    const inputPath = `${INPUT_DIR}/${sessionId}.in`;
    if (!existsSync(inputPath)) await writeFile(inputPath, "");
    for await (const m of query({ prompt: userMessages(inputPath), options })) emit(m);
    return;
  }

  emit({ type: "error", error: "fc-agent-runner: provide --prompt <text> or --session <id>" });
  process.exit(2);
}

main().catch((err) => {
  emit({
    type: "error",
    error: err && err.message ? err.message : String(err),
    stack: err && err.stack ? String(err.stack).slice(0, 1500) : undefined,
  });
  process.exit(1);
});
