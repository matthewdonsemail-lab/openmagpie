#!/usr/bin/env node
/**
 * GitHub Repo → Buzz Bridge
 *
 * Receives OpenMagpie webhook payloads (WebhookPayload shape) from the
 * github_events or github_search connectors and posts them to the
 * ★ | GitHubs Buzz channel using Matt's personal key via NIP-98 HTTP auth.
 *
 * No Buzz CLI needed — uses nostr-tools directly over HTTP POST to
 * {relay}/events with NIP-98 HTTP auth (kind 27235).
 *
 * Usage:
 *   node /app/github-repo-buzz-bridge.mjs [--port 9879]
 *
 * To test:
 *   curl -X POST http://localhost:9879/hook -H "Content-Type: application/json" -d @test-payload.json
 */

import { createServer } from "node:http";
import { randomUUID, createHash } from "node:crypto";
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { finalizeEvent, getPublicKey, utils } from "nostr-tools";
import { decode } from "nostr-tools/nip19";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ENV_PATH = resolve(__dirname, "..", ".env.local");

// ── Config ──────────────────────────────────────────────────────────────────
const PORT = parseInt(process.argv.find(a => a.startsWith("--port="))?.split("=")[1] ?? process.env.PORT ?? "9879", 10);
const GITHUBS_CHANNEL_ID = "3cbcd95c-831b-4ae9-8971-d13b0cb22e8e";

// ── Load secrets from .env.local ────────────────────────────────────────────
function loadEnv(path) {
  if (!existsSync(path)) {
    console.error("FATAL: .env.local not found at", path);
    process.exit(1);
  }
  const text = readFileSync(path, "utf8");
  const vars = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let val = trimmed.slice(eq + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    vars[key] = val;
  }
  return vars;
}

const env = loadEnv(ENV_PATH);
const BUZZ_PRIVATE_KEY = env.MATT_PRIVATE_KEY || env.BUZZ_PRIVATE_KEY;
const BUZZ_RELAY_URL = env.BUZZ_RELAY_URL;

if (!BUZZ_PRIVATE_KEY) {
  console.error("FATAL: No BUZZ_PRIVATE_KEY or MATT_PRIVATE_KEY found in .env.local");
  process.exit(1);
}
if (!BUZZ_RELAY_URL) {
  console.error("FATAL: No BUZZ_RELAY_URL found in .env.local");
  process.exit(1);
}

// ── Resolve key bytes ───────────────────────────────────────────────────────
let sk = BUZZ_PRIVATE_KEY;
if (sk.startsWith("nsec1")) {
  sk = utils.bytesToHex(decode(sk).data);
}
const skBytes = utils.hexToBytes(sk);
const pubkey = getPublicKey(skBytes);

// ── Normalise relay URL to HTTPS ────────────────────────────────────────────
const relayUrl = BUZZ_RELAY_URL
  .replace(/\/+$/, "")
  .replace(/^ws:\/\//, "http://")
  .replace(/^wss:\/\//, "https://");

// ── Sign a NIP-98 HTTP auth event (kind 27235) ──────────────────────────────
function signNip98(method, url, bodyBytes) {
  const tags = [
    ["u", url],
    ["method", method],
    ["nonce", randomUUID()],
  ];
  if (bodyBytes) {
    const hash = createHash("sha256").update(bodyBytes).digest("hex");
    tags.push(["payload", hash]);
  }
  const event = finalizeEvent(
    {
      kind: 27235,
      created_at: Math.floor(Date.now() / 1000),
      tags,
      content: "",
    },
    skBytes,
  );
  return Buffer.from(JSON.stringify(event)).toString("base64");
}

// ── Send a kind-9 event to the relay via HTTP POST ──────────────────────────
async function sendToBuzz(content) {
  // Neutralize @-mentions
  content = content.replace(/@/g, "@\u200B");

  const tags = [["h", GITHUBS_CHANNEL_ID]];
  const event = finalizeEvent(
    {
      kind: 9,
      created_at: Math.floor(Date.now() / 1000),
      tags,
      content,
    },
    skBytes,
  );

  const bodyBytes = Buffer.from(JSON.stringify(event));
  const eventsUrl = `${relayUrl}/events`;
  const nip98Auth = signNip98("POST", eventsUrl, bodyBytes);

  const controller = new AbortController();
  const timeoutHandle = setTimeout(() => controller.abort(), 30_000);

  try {
    const response = await fetch(eventsUrl, {
      method: "POST",
      headers: {
        Authorization: `Nostr ${nip98Auth}`,
        "Content-Type": "application/json",
      },
      body: bodyBytes,
      signal: controller.signal,
    });

    const text = await response.text();
    clearTimeout(timeoutHandle);

    if (!response.ok) {
      throw new Error(`relay rejected (HTTP ${response.status}): ${text.slice(0, 300)}`);
    }

    return { event_id: event.id, pubkey };
  } finally {
    clearTimeout(timeoutHandle);
  }
}

// ── Format a repo item into a Buzz message ──────────────────────────────────
function formatRepoMessage(item, watchName) {
  const data = item.item || {};
  const source = item.source || {};
  const key = item.key || "";

  const sourceKind = source.kind || "";
  const isEvent = sourceKind === "github_events";

  // ONLY events — search results are dead
  if (!isEvent) return null;

  const fullName = data.full_name || data.title || key.split(":")[1] || "Unknown repo";
  const url = data.url || `https://github.com/${fullName}`;
  const stars = data.stars ?? 0;
  const forks = data.forks ?? 0;
  const language = data.language || "";
  const description = data.content || "";
  const owner = data.owner || fullName.split("/")[0] || "";
  const topics = Array.isArray(data.topics) ? data.topics : [];
  const licenseName = data.license_name || "";
  const openIssues = data.open_issues ?? 0;

  // Actor (person who triggered the event)
  const actorLogin = data.actor_login || "";
  const actorAvatarUrl = data.actor_avatar_url || "";

  // Skip if no actor (shouldn't happen for events, but safety)
  if (!actorLogin) return null;

  // ── AI/LLM/agent filter: check topics, description, language ──
  const aiKeywords = ["ai", "llm", "agent", "openai", "gpt", "claude", "gemini",
    "deepseek", "langchain", "rag", "embedding", "transformer", "neural",
    "machine learning", "ml", "chatbot", "copilot", "inference", "model"];

  const lowerDesc = description.toLowerCase();
  const lowerLang = language.toLowerCase();
  const lowerTopics = topics.map(t => t.toLowerCase());

  const hasAiSignal = aiKeywords.some(kw =>
    lowerTopics.includes(kw) ||
    lowerDesc.includes(kw) ||
    lowerLang.includes(kw)
  );

  if (!hasAiSignal) {
    console.log(`  SKIP ${fullName}: no AI/LLM/agent signal in topics/description`);
    return null;
  }

  const eventType = data.event_type || "create";
  const verb = eventType === "push" ? "pushed to" : "created";

  const lines = [];
  lines.push(`**${actorLogin}** — ${verb} **${fullName}**`);
  lines.push(`🔗 https://github.com/${actorLogin}`);
  lines.push("");  // blank line before repo section
  lines.push(`📦 **${fullName}**`);
  if (description) {
    const desc = description.length > 200 ? description.slice(0, 200) + "…" : description;
    lines.push(`> ${desc}`);
  }
  const meta = [];
  if (stars > 0) meta.push(`⭐ ${stars.toLocaleString()}`);
  if (forks > 0) meta.push(`🍴 ${forks.toLocaleString()}`);
  if (language) meta.push(`🔤 ${language}`);
  if (licenseName) meta.push(`📄 ${licenseName}`);
  if (meta.length) lines.push(meta.join(" · "));
  if (topics.length > 0) {
    lines.push(`🏷️ ${topics.slice(0, 8).join(", ")}${topics.length > 8 ? ` +${topics.length - 8}` : ""}`);
  }
  lines.push(`🔗 ${url}`);

  return lines.join("\n");
}

// ── HTTP handler ────────────────────────────────────────────────────────────
const server = createServer(async (req, res) => {
  // CORS for development
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    return res.end();
  }

  if (req.method !== "POST") {
    res.writeHead(405, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ error: "Method not allowed — POST only" }));
  }

  // Parse the body
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  let body;
  try {
    body = JSON.parse(Buffer.concat(chunks).toString() || "{}");
  } catch {
    res.writeHead(400, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ error: "Invalid JSON body" }));
  }

  // Extract items from the WebhookPayload
  const items = body.items || [];
  const watchName = body.watch?.name || "github-repo-radar";

  if (items.length === 0) {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, posted: 0, reason: "No items in payload" }));
  }

  // Post each item as a separate message
  const results = [];
  for (const item of items) {
    try {
      const content = formatRepoMessage(item, watchName);
      if (!content) {
        results.push({ key: item.key, status: "skipped", reason: "no AI/LLM/agent signal" });
        continue;
      }
      const buzzResult = await sendToBuzz(content);
      results.push({ key: item.key, status: "posted", event_id: buzzResult.event_id });
    } catch (err) {
      results.push({ key: item.key, status: "failed", error: err.message.slice(0, 200) });
    }
  }

  const posted = results.filter(r => r.status === "posted").length;
  const failed = results.filter(r => r.status === "failed").length;

  console.log(`[${new Date().toISOString()}] GitHub → Buzz: ${posted} posted, ${failed} failed`);

  res.writeHead(failed > 0 && posted === 0 ? 502 : 200, { "Content-Type": "application/json" });
  return res.end(JSON.stringify({ ok: failed === 0, posted, failed, results }));
});

server.listen(PORT, () => {
  console.log(`🚀 GitHub → Buzz Bridge listening on http://localhost:${PORT}`);
  console.log(`   Channel: ★ | GitHubs (${GITHUBS_CHANNEL_ID})`);
  console.log(`   Using key: ${BUZZ_PRIVATE_KEY.slice(0, 12)}…`);
  console.log(`   Relay: ${BUZZ_RELAY_URL}`);
  console.log(`   Endpoint: POST http://localhost:${PORT}/hook`);
});

// Handle graceful shutdown
process.on("SIGINT", () => { console.log("\nShutting down…"); server.close(); process.exit(0); });
process.on("SIGTERM", () => { server.close(); process.exit(0); });
