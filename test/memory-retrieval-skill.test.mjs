import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const CODEX_SCRIPT = join(
  ROOT,
  "integrations",
  "codex",
  "memory-retrieval",
  "scripts",
  "recall-memory.mjs",
);
const CLAUDE_SCRIPT = join(
  ROOT,
  "integrations",
  "claude-code",
  "memory-retrieval",
  "scripts",
  "recall-memory.mjs",
);
const INSTALLER = join(ROOT, "integrations", "install-memory-retrieval-skill.mjs");

function runNode(script, args) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [script, ...args], {
      cwd: ROOT,
      env: { ...process.env },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

async function withServer(handler, callback) {
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try {
    await callback(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    });
  }
}

test("Codex and Claude Code ship the same deterministic recall tool", async () => {
  const [codex, claude] = await Promise.all([
    readFile(CODEX_SCRIPT, "utf8"),
    readFile(CLAUDE_SCRIPT, "utf8"),
  ]);
  assert.equal(codex, claude);
});

for (const script of [CODEX_SCRIPT, CLAUDE_SCRIPT]) {
  test(`${script}: requests ten memories by default and preserves explicit limits`, async () => {
    const requests = [];
    await withServer(async (request, response) => {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      requests.push(body);
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({
        contexts: Array.from({ length: body.top_k }, (_, index) => ({
          source_type: "knowledge", knowledge_id: index, text: `记忆 ${index}`,
        })),
      }));
    }, async (baseUrl) => {
      for (const [args, expected] of [[[], 10], [["--top-k", "3"], 3]]) {
        const result = await runNode(script, ["--query", "项目决策", "--base-url", baseUrl, ...args]);
        assert.equal(result.code, 0, result.stderr);
        const payload = JSON.parse(result.stdout);
        assert.equal(payload.result_count, expected);
        assert.equal(payload.contexts.length, expected);
        assert.deepEqual(requests.at(-1), { query: "项目决策", top_k: expected });
      }
      const invalid = await runNode(script, ["--query", "项目决策", "--base-url", baseUrl, "--top-k", "11"]);
      assert.equal(invalid.code, 2);
      assert.equal(JSON.parse(invalid.stderr).error.code, "INVALID_ARGUMENT");
      assert.equal(requests.length, 2);
    });
  });
}

test("normalizes recall results and truncates oversized text", async () => {
  await withServer((request, response) => {
    if (request.url !== "/api/rag/references" || request.method !== "POST") {
      response.writeHead(404).end();
      return;
    }
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({
      answer: "",
      model: "references-only",
      contexts: [{
        capture_id: 42,
        source_type: "knowledge",
        title: "项目回顾",
        text: "甲".repeat(240),
        score: 0.91,
        observed_at: "2026-07-20T09:30:00+08:00",
        screenshot_path: "/private/path/should-not-leak.png",
      }],
    }));
  }, async (baseUrl) => {
    const result = await runNode(CODEX_SCRIPT, [
      "--query",
      "上次项目如何决策",
      "--top-k",
      "3",
      "--max-chars",
      "200",
      "--base-url",
      baseUrl,
    ]);

    assert.equal(result.code, 0, result.stderr);
    const payload = JSON.parse(result.stdout);
    assert.equal(payload.schema_version, "memorybread.recall.v1");
    assert.equal(payload.result_count, 1);
    assert.equal(payload.contexts[0].memory_id, "knowledge:42");
    assert.equal(payload.contexts[0].truncated, true);
    assert.equal(payload.contexts[0].text.length, 201);
    assert.equal("screenshot_path" in payload.contexts[0], false);
  });
});

test("checks local service health", async () => {
  await withServer((_request, response) => {
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({ status: "ok" }));
  }, async (baseUrl) => {
    const result = await runNode(CLAUDE_SCRIPT, ["--check", "--base-url", baseUrl]);
    assert.equal(result.code, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).status, "ready");
  });
});

test("preserves model-not-ready semantics through the Core Engine gateway", async () => {
  await withServer((_request, response) => {
    response.statusCode = 503;
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({
      error: "SERVICE_UNAVAILABLE",
      message: "向量模型未就绪，请前往模型界面检查状态",
    }));
  }, async (baseUrl) => {
    const result = await runNode(CODEX_SCRIPT, [
      "--query",
      "上次项目如何决策",
      "--base-url",
      baseUrl,
    ]);
    assert.equal(result.code, 4);
    assert.equal(JSON.parse(result.stderr).error.code, "MODEL_NOT_READY");
  });
});

test("rejects remote endpoints before making a request", async () => {
  const result = await runNode(CODEX_SCRIPT, [
    "--query",
    "private memory",
    "--base-url",
    "https://example.com",
  ]);
  assert.equal(result.code, 2);
  assert.equal(JSON.parse(result.stderr).error.code, "REMOTE_ENDPOINT_REJECTED");
});

test("installer dry run reports both platform destinations", async () => {
  const result = await runNode(INSTALLER, ["both", "--dry-run"]);
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /\.agents[/\\]skills[/\\]memory-retrieval/);
  assert.match(result.stdout, /\.claude[/\\]skills[/\\]memory-retrieval/);
});
