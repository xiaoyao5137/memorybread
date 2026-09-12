#!/usr/bin/env node

import process from "node:process";

const DEFAULT_BASE_URL = "http://127.0.0.1:7070";
const DEFAULT_TOP_K = 10;
const DEFAULT_MAX_CHARS = 3000;
const DEFAULT_TIMEOUT_MS = 90_000;
const MAX_QUERY_CHARS = 2000;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

class RecallError extends Error {
  constructor(code, message, hint, exitCode = 1) {
    super(message);
    this.code = code;
    this.hint = hint;
    this.exitCode = exitCode;
  }
}

function usage() {
  return `MemoryBread 记忆检索

Usage:
  node recall-memory.mjs --query "<focused query>" [--top-k 10]
  node recall-memory.mjs --check

Options:
  -q, --query <text>       Focused recall query. Reads stdin when omitted.
      --top-k <1-10>       Maximum memories to return. Default: 10.
      --max-chars <200-8000>
                           Maximum text characters per result. Default: 3000.
      --timeout-ms <1000-120000>
                           Request timeout. Default: 90000.
      --base-url <url>     Loopback MemoryBread URL. Default: ${DEFAULT_BASE_URL}.
      --check              Check the local service without recalling memory.
  -h, --help               Show this help.

Environment:
  MEMORY_BREAD_LOCAL_URL
  MEMORY_BREAD_RECALL_TIMEOUT_MS
`;
}

function failArgument(message, hint = "Correct the command arguments and retry.") {
  throw new RecallError("INVALID_ARGUMENT", message, hint, 2);
}

function parseInteger(value, name, min, max) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    failArgument(`${name} must be an integer between ${min} and ${max}.`);
  }
  return parsed;
}

function parseArgs(argv) {
  const options = {
    query: "",
    topK: DEFAULT_TOP_K,
    maxChars: DEFAULT_MAX_CHARS,
    timeoutMs: parseInteger(
      process.env.MEMORY_BREAD_RECALL_TIMEOUT_MS || String(DEFAULT_TIMEOUT_MS),
      "MEMORY_BREAD_RECALL_TIMEOUT_MS",
      1000,
      120_000,
    ),
    baseUrl: process.env.MEMORY_BREAD_LOCAL_URL || DEFAULT_BASE_URL,
    check: false,
    help: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = () => {
      index += 1;
      if (index >= argv.length) failArgument(`${arg} requires a value.`);
      return argv[index];
    };

    if (arg === "--query" || arg === "-q") options.query = next();
    else if (arg === "--top-k") options.topK = parseInteger(next(), "--top-k", 1, 10);
    else if (arg === "--max-chars") options.maxChars = parseInteger(next(), "--max-chars", 200, 8000);
    else if (arg === "--timeout-ms") options.timeoutMs = parseInteger(next(), "--timeout-ms", 1000, 120_000);
    else if (arg === "--base-url") options.baseUrl = next();
    else if (arg === "--check") options.check = true;
    else if (arg === "--help" || arg === "-h") options.help = true;
    else failArgument(`Unknown argument: ${arg}`, "Run with --help to see supported options.");
  }

  return options;
}

function validateBaseUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    failArgument(
      "MEMORY_BREAD_LOCAL_URL is not a valid URL.",
      `Use a loopback URL such as ${DEFAULT_BASE_URL}.`,
    );
  }

  if (
    url.protocol !== "http:"
    || !LOOPBACK_HOSTS.has(url.hostname.toLowerCase())
    || url.username
    || url.password
    || (url.pathname !== "/" && url.pathname !== "")
    || url.search
    || url.hash
  ) {
    throw new RecallError(
      "REMOTE_ENDPOINT_REJECTED",
      "The memory recall tool only connects to a loopback HTTP endpoint.",
      `Use ${DEFAULT_BASE_URL} or another localhost port.`,
      2,
    );
  }

  return url.toString().replace(/\/$/, "");
}

async function readStdin() {
  if (process.stdin.isTTY) return "";
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8").trim();
}

function cleanString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function normalizeContext(context, maxChars) {
  const sourceType = cleanString(context.source_type) || cleanString(context.source) || "memory";
  const rawText = cleanString(context.text)
    || cleanString(context.summary)
    || cleanString(context.overview)
    || "";
  const truncated = rawText.length > maxChars;
  const text = truncated ? `${rawText.slice(0, maxChars).trimEnd()}…` : rawText;
  const sourceId = firstDefined(
    context.artifact_id,
    context.knowledge_id,
    context.document_id,
    context.capture_id,
    context.doc_key,
  );

  return Object.fromEntries(Object.entries({
    memory_id: sourceId === undefined ? undefined : `${sourceType}:${sourceId}`,
    source_type: sourceType,
    title: cleanString(context.title)
      || cleanString(context.summary)
      || cleanString(context.win_title),
    text,
    truncated,
    score: Number.isFinite(Number(context.score)) ? Number(context.score) : undefined,
    time: firstDefined(
      context.observed_at,
      context.event_time_end,
      context.end_time,
      context.time,
      context.event_time_start,
      context.start_time,
    ),
    app_name: cleanString(context.app_name),
    window_title: cleanString(context.win_title),
    url: cleanString(context.url) || cleanString(context.source_url),
    category: cleanString(context.category),
    activity_type: cleanString(context.activity_type),
  }).filter(([, value]) => value !== undefined));
}

function classifyServiceError(status, payload) {
  const upstreamCode = cleanString(payload?.error);
  const upstreamMessage = cleanString(payload?.message) || upstreamCode;
  const modelNotReady = upstreamCode === "MODEL_NOT_READY"
    || /(?:向量|召回|embedding).{0,12}(?:模型)?.{0,8}(?:未就绪|not ready)/iu.test(upstreamMessage || "");

  if (modelNotReady) {
    return new RecallError(
      "MODEL_NOT_READY",
      "MemoryBread's local recall model is not ready.",
      "Open MemoryBread and check the local embedding model status.",
      4,
    );
  }
  if (status === 504) {
    return new RecallError(
      "TIMEOUT",
      "MemoryBread memory recall timed out.",
      "Retry once with a narrower query.",
      5,
    );
  }
  if (status === 502 || status === 503) {
    return new RecallError(
      "SERVICE_UNAVAILABLE",
      upstreamMessage || "MemoryBread memory recall is temporarily unavailable.",
      "Start MemoryBread and wait for local services to become ready.",
      3,
    );
  }
  return new RecallError(
    "SERVICE_ERROR",
    `MemoryBread returned HTTP ${status}.`,
    "Check MemoryBread's local service status and retry.",
    4,
  );
}

async function requestJson(url, init, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    const raw = await response.text();
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        throw new RecallError(
          "INVALID_RESPONSE",
          "MemoryBread returned a non-JSON response.",
          "Treat recall as unavailable and check the local service logs.",
          4,
        );
      }
    }
    if (!response.ok) throw classifyServiceError(response.status, payload);
    return payload;
  } catch (error) {
    if (error instanceof RecallError) throw error;
    if (error?.name === "AbortError") {
      throw new RecallError(
        "TIMEOUT",
        "MemoryBread memory recall timed out.",
        "Retry once with a narrower query.",
        5,
      );
    }
    throw new RecallError(
      "SERVICE_UNAVAILABLE",
      "Could not connect to the local MemoryBread service.",
      "Start MemoryBread and wait for local services to become ready.",
      3,
    );
  } finally {
    clearTimeout(timer);
  }
}

function writeJson(stream, payload) {
  stream.write(`${JSON.stringify(payload, null, 2)}\n`);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(usage());
    return;
  }

  const baseUrl = validateBaseUrl(options.baseUrl);

  if (options.check) {
    const payload = await requestJson(`${baseUrl}/health`, { method: "GET" }, options.timeoutMs);
    writeJson(process.stdout, {
      schema_version: "memorybread.recall.health.v1",
      status: payload?.status === "ok" ? "ready" : "reachable",
      service_url: baseUrl,
    });
    return;
  }

  const query = (options.query || await readStdin()).trim();
  if (!query) {
    failArgument(
      "A recall query is required.",
      "Pass --query or pipe a focused query through stdin.",
    );
  }
  if (query.length > MAX_QUERY_CHARS) {
    failArgument(
      `The recall query exceeds ${MAX_QUERY_CHARS} characters.`,
      "Use a shorter query focused on one task, project, or decision.",
    );
  }

  const payload = await requestJson(
    `${baseUrl}/api/rag/references`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query, top_k: options.topK }),
    },
    options.timeoutMs,
  );

  if (!Array.isArray(payload?.contexts)) {
    throw new RecallError(
      "INVALID_RESPONSE",
      "MemoryBread returned an invalid recall response.",
      "Treat recall as unavailable and check the local service logs.",
      4,
    );
  }

  const contexts = payload.contexts.map((context) => normalizeContext(context || {}, options.maxChars));
  writeJson(process.stdout, {
    schema_version: "memorybread.recall.v1",
    query,
    result_count: contexts.length,
    contexts,
  });
}

main().catch((error) => {
  const normalized = error instanceof RecallError
    ? error
    : new RecallError(
      "UNEXPECTED_ERROR",
      "The memory recall tool failed unexpectedly.",
      "Check the command and local service logs.",
      1,
    );
  writeJson(process.stderr, {
    schema_version: "memorybread.recall.error.v1",
    error: {
      code: normalized.code,
      message: normalized.message,
      hint: normalized.hint,
    },
  });
  process.exitCode = normalized.exitCode;
});
