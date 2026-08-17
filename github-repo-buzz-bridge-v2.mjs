#!/usr/bin/env node
/**
 * GitHub Repo → Buzz Bridge — v2 (GitHub Social Listener)
 *
 * Receives OpenMagpie webhook payloads (WebhookPayload shape) from the
 * github_events connector and posts a structured qualification event to the
 * ★ | GitHubs Buzz channel using Matt's personal key via NIP-98 HTTP auth.
 *
 * Stage scope (per funnel spec): identify action -> identify person ->
 * enrich GitHub profile (public email, bio links) -> identify website/portfolio
 * URL (NO website crawling at this stage) -> extract GitHub + profile signals ->
 * score -> rank -> determine status -> next action -> output structured event.
 *
 * Output format (markdown):
 *   ACTION: PUSHED
 *   PERSON / GITHUB ACTIVITY / SIGNALS / QUALIFICATION / NEXT ACTION / README(code block)
 *
 * Usage:
 *   node /app/github-repo-buzz-bridge.mjs [--port 9879]
 *
 * Env (all optional, resolved in order: process.env, apps/core/.env, .env.local):
 *   BUZZ_RELAY_URL, MATT_PRIVATE_KEY/BUZZ_PRIVATE_KEY,
 *   GITHUB_TOKEN (profile enrichment), ENGINE_BASE_URL/ENGINE_API_KEY/ENGINE_MODEL (reason)
 */

import { createServer } from "node:http";
import { randomUUID, createHash } from "node:crypto";
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { finalizeEvent, getPublicKey, utils } from "nostr-tools";
import { decode } from "nostr-tools/nip19";

const __dirname = dirname(fileURLToPath(import.meta.url));

// -- Config --
const PORT = parseInt(process.argv.find(a => a.startsWith("--port="))?.split("=")[1] ?? process.env.PORT ?? "9879", 10);
const GITHUBS_CHANNEL_ID = "3cbcd95c-831b-4ae9-8971-d13b0cb22e8e";

// -- Load env files --
function loadEnv(path) {
  if (!existsSync(path)) return {};
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

const localEnv = loadEnv(resolve(__dirname, ".env.local"));
const parentEnv = loadEnv(resolve(__dirname, "..", ".env.local"));
const coreEnv = loadEnv(resolve(__dirname, "apps", "core", ".env"));
const env = { ...coreEnv, ...parentEnv, ...localEnv, ...process.env };

const BUZZ_PRIVATE_KEY = env.MATT_PRIVATE_KEY || env.BUZZ_PRIVATE_KEY;
const BUZZ_RELAY_URL = env.BUZZ_RELAY_URL;
const GITHUB_TOKEN = env.GITHUB_TOKEN || "";
const ENGINE_BASE_URL = (env.ENGINE_BASE_URL || "https://api.inferencesaver.com/v1").replace(/\/+$/, "");
const ENGINE_API_KEY = env.ENGINE_API_KEY || "";
const ENGINE_MODEL = env.ENGINE_MODEL || "agnes-2.0-flash";

if (!BUZZ_PRIVATE_KEY) {
  console.error("FATAL: No BUZZ_PRIVATE_KEY or MATT_PRIVATE_KEY found in .env.local");
  process.exit(1);
}
if (!BUZZ_RELAY_URL) {
  console.error("FATAL: No BUZZ_RELAY_URL found in .env.local");
  process.exit(1);
}

// -- Resolve key bytes --
let sk = BUZZ_PRIVATE_KEY;
if (sk.startsWith("nsec1")) {
  sk = utils.bytesToHex(decode(sk).data);
}
const skBytes = utils.hexToBytes(sk);
const pubkey = getPublicKey(skBytes);

// -- Normalise relay URL to HTTPS --
const relayUrl = BUZZ_RELAY_URL
  .replace(/\/+$/, "")
  .replace(/^ws:\/\//, "http://")
  .replace(/^wss:\/\//, "https://");

// -- NIP-98 HTTP auth (kind 27235) --
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
    { kind: 27235, created_at: Math.floor(Date.now() / 1000), tags, content: "" },
    skBytes,
  );
  return Buffer.from(JSON.stringify(event)).toString("base64");
}

// -- Send a kind-9 event to the relay via HTTP POST --
async function sendToBuzz(content) {
  content = content.replace(/@/g, "@\u200B");
  const tags = [["h", GITHUBS_CHANNEL_ID]];
  const event = finalizeEvent(
    { kind: 9, created_at: Math.floor(Date.now() / 1000), tags, content },
    skBytes,
  );
  const bodyBytes = Buffer.from(JSON.stringify(event));
  const eventsUrl = relayUrl + "/events";
  const nip98Auth = signNip98("POST", eventsUrl, bodyBytes);
  const controller = new AbortController();
  const timeoutHandle = setTimeout(() => controller.abort(), 30_000);
  try {
    const response = await fetch(eventsUrl, {
      method: "POST",
      headers: { Authorization: "Nostr " + nip98Auth, "Content-Type": "application/json" },
      body: bodyBytes,
      signal: controller.signal,
    });
    const text = await response.text();
    clearTimeout(timeoutHandle);
    if (!response.ok) {
      throw new Error("relay rejected (HTTP " + response.status + "): " + text.slice(0, 300));
    }
    return { event_id: event.id, pubkey };
  } finally {
    clearTimeout(timeoutHandle);
  }
}

// -- GitHub API helpers --
const GH_HEADERS = {
  Accept: "application/vnd.github+json",
  "User-Agent": "openmagpie-github-bridge/2.0 (+https://github.com/obris-dev/openmagpie)",
  ...(GITHUB_TOKEN ? { Authorization: "Bearer " + GITHUB_TOKEN } : {}),
};

async function fetchGitHubUser(login) {
  const res = await fetch("https://api.github.com/users/" + encodeURIComponent(login), {
    headers: GH_HEADERS,
    signal: AbortSignal.timeout(15_000),
  });
  if (res.status === 404) return null;
  if (res.status === 403) {
    console.log("  GITHUB RATE LIMIT: user fetch for " + login + " blocked (403)");
    return null;
  }
  if (!res.ok) throw new Error("GitHub user fetch failed (" + res.status + ") for " + login);
  return res.json();
}

// -- Extraction helpers --
const EMAIL_RE = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g;
const URL_RE = /https?:\/\/[^\s)\]>"']+/g;
const MD_LINK_RE = /\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g;

const SOCIAL_HOSTS = new Set([
  "twitter.com", "x.com", "linkedin.com", "www.linkedin.com", "facebook.com", "instagram.com",
  "t.me", "telegram.me", "youtube.com", "www.youtube.com", "twitch.tv", "tiktok.com",
]);

function isSocialUrl(rawHost) {
  const host = rawHost.replace(/^www\./, "").toLowerCase();
  for (const s of SOCIAL_HOSTS) {
    if (host === s || host.endsWith("." + s)) return true;
  }
  return false;
}

function extractUrls(text) {
  const urls = new Set();
  if (!text) return [];
  for (const m of text.matchAll(MD_LINK_RE)) urls.add(m[2]);
  for (const m of text.matchAll(URL_RE)) urls.add(m[0].replace(/[.,;:\u2026]+$/, ""));
  return [...urls];
}

function normalizeUrl(raw) {
  let u = (raw || "").trim();
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  return u;
}

function pickWebsite(blog, bioUrls) {
  const candidates = [];
  if (blog) candidates.push(normalizeUrl(blog));
  for (const u of (bioUrls || [])) {
    if (!candidates.includes(u)) candidates.push(u);
  }
  for (const u of candidates) {
    try {
      const host = new URL(u).hostname;
      if (!isSocialUrl(host)) return u;
    } catch { /* skip unparseable */ }
  }
  return candidates[0] || "";
}

// -- Signal keywords (ICP: AI / LLM / agents / inference) --
const AI_KEYWORDS = [
  "ai", "llm", "llms", "agent", "agents", "openai", "gpt", "gpt-4", "chatgpt", "claude",
  "anthropic", "gemini", "deepseek", "langchain", "llamaindex", "rag", "embedding",
  "transformer", "neural", "machine learning", "machine-learning", "ml", "chatbot",
  "copilot", "inference", "model", "fine-tuning", "finetuning", "prompt", "generative",
  "genai", "gen-ai", "agi", "multimodal", "diffusion", "autonomous", "nlp", "pytorch",
  "tensorflow", "huggingface", "vector db", "vector-db",
];

function findSignals(text) {
  const found = [];
  if (!text) return found;
  const lower = text.toLowerCase();
  for (const kw of AI_KEYWORDS) {
    if (lower.includes(kw)) found.push(kw.toUpperCase());
  }
  return [...new Set(found)];
}

// -- LLM-generated qualification reason (non-fatal) --
async function generateReason(context) {
  if (!ENGINE_API_KEY) return "";
  const prompt = "You are the qualification explainer in a GitHub social-listener funnel for an AI infrastructure company. " +
    "Given these extracted signals, write ONE concise sentence (max 30 words) explaining why this GitHub actor " +
    "was scored " + context.score + "/100 and ranked " + context.ranking + ". Base it ONLY on the listed signals. " +
    "No markdown, no preamble.\n\nSignal JSON:\n" + JSON.stringify(context, null, 2);
  try {
    const res = await fetch(ENGINE_BASE_URL + "/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(ENGINE_API_KEY ? { Authorization: "Bearer " + ENGINE_API_KEY } : {}),
      },
      body: JSON.stringify({
        model: ENGINE_MODEL,
        messages: [
          { role: "system", content: "You write terse, evidence-based qualification reasons." },
          { role: "user", content: prompt },
        ],
        max_tokens: 300,
        temperature: 0.3,
      }),
      signal: AbortSignal.timeout(20_000),
    });
    if (!res.ok) throw new Error("engine " + res.status);
    const json = await res.json();
    return (json.choices?.[0]?.message?.content || "").trim();
  } catch (err) {
    console.log("  [warn] reason generation failed: " + err.message);
    return "";
  }
}

// -- Qualification: score -> rank -> status -> next action --
function qualify(opts) {
  let score = 0;
  const parts = [];
  const repoPts = Math.min(40, opts.githubSignals.length * 8);
  score += repoPts;
  if (opts.githubSignals.length) parts.push("repo signals (" + opts.githubSignals.join(", ") + ")");

  const profilePts = Math.min(25, opts.profileSignals.length * 10);
  score += profilePts;
  if (opts.profileSignals.length) parts.push("profile signals (" + opts.profileSignals.join(", ") + ")");

  if (opts.hasEmail) { score += 10; parts.push("public email found"); }
  if (opts.website) { score += 15; parts.push("website/portfolio identified"); }

  score += Math.min(10, opts.profileCompleteness * 3);
  score = Math.min(100, score);

  let ranking, status, nextAction;
  if (score >= 70) {
    ranking = "HIGH PRIORITY";
    status = "QUALIFIED";
    nextAction = "Pass to website/portfolio enrichment -> outreach queue";
  } else if (score >= 50) {
    ranking = "MEDIUM PRIORITY";
    status = "QUALIFIED";
    nextAction = "Send to enrichment queue for deeper qualification";
  } else {
    ranking = "BELOW THRESHOLD";
    status = "FILTERED";
    nextAction = "Not posted (score below 50 gate)";
  }

  return { score, ranking, status, nextAction, evidence: parts };
}

// -- Enrich profile --
async function enrichProfile(login) {
  const user = await fetchGitHubUser(login);
  if (!user) {
    return { user: null, email: "", bioUrls: [], website: "", profileSignals: [], completeness: 0 };
  }
  const bio = user.bio || "";
  const bioUrls = extractUrls(bio);
  const website = pickWebsite(user.blog, bioUrls);
  const email = (user.email || "").match(EMAIL_RE)?.[0] || bio.match(EMAIL_RE)?.[0] || "";
  const profileText = [bio, user.name, user.company, user.location].filter(Boolean).join(" · ");
  const profileSignals = findSignals(profileText);
  const completeness = [user.name, bio, user.company, user.location].filter(Boolean).length;
  return { user, email, bioUrls, website, profileSignals, completeness };
}

// -- Format the structured event --
async function formatRepoMessage(item) {
  const data = item.item || {};
  const source = item.source || {};
  const key = item.key || "";
  const sourceKind = source.kind || "";

  // ONLY github_events -- search results are dead
  if (sourceKind !== "github_events") return null;

  const fullName = data.full_name || data.title || key.split(":")[1] || "Unknown repo";
  const url = data.url || "https://github.com/" + fullName;
  const description = data.content || "";
  const language = data.language || "";
  const topics = Array.isArray(data.topics) ? data.topics : [];
  const readme = data.readme || "";
  const actorLogin = data.actor_login || "";

  if (!actorLogin) return null;

  // Pre-filter (conservative): topics / description / language
  const prefText = [topics.join(" "), description, language].join(" · ");
  const prefSignals = findSignals(prefText);
  if (prefSignals.length === 0) {
    console.log("  SKIP " + fullName + ": no AI/LLM/agent signal in topics/description/language");
    return { skipped: true, key: item.key, reason: "no AI/LLM/agent signal" };
  }

  // GitHub activity signals (include readme for richer signal set)
  const ghText = [fullName, description, language, topics.join(" "), readme].join(" · ");
  const githubSignals = findSignals(ghText);

  const eventType = data.event_type || "create";
  const actionWord = eventType === "push" ? "PUSHED" : "CREATED";
  const actionVerb = eventType === "push" ? "Pushed" : "Created";

  // Enrich GitHub profile (non-fatal)
  let profile = { user: null, email: "", bioUrls: [], website: "", profileSignals: [], completeness: 0 };
  try {
    profile = await enrichProfile(actorLogin);
  } catch (err) {
    console.log("  [warn] profile enrichment failed for " + actorLogin + ": " + err.message);
  }

  const { user, email, bioUrls, website, profileSignals, completeness } = profile;

  const allProfileLinks = [...bioUrls];
  if (user && user.blog && !allProfileLinks.includes(normalizeUrl(user.blog))) {
    allProfileLinks.unshift(normalizeUrl(user.blog));
  }

  const q = qualify({
    githubSignals: githubSignals.slice(0, 6),
    profileSignals: profileSignals.slice(0, 6),
    hasEmail: Boolean(email),
    website,
    profileCompleteness: completeness,
  });

  // HARD GATE: only leads scoring 50+ get posted to the channel. Anything below
  // is suppressed entirely (still logged) -- no COLD/IGNORED entries in the feed.
  if (q.score < 50) {
    console.log("  SKIP " + fullName + ": score " + q.score + "/100 below 50 gate");
    return { skipped: true, key: item.key, reason: "score " + q.score + "/100 below 50 gate" };
  }

  const reason = (await generateReason({
    score: q.score,
    ranking: q.ranking,
    login: actorLogin,
    repo: fullName,
    signals: {
      github: githubSignals.slice(0, 8),
      profile: profileSignals.slice(0, 8),
      website,
      email: email || null,
    },
  })) || "Evidence: " + (q.evidence.length ? q.evidence.join("; ") : "no signals beyond gating match") + ".";

  const actionEmoji = eventType === "push" ? "🚀" : "🆕";
  const rankingEmoji = q.ranking === "HIGH PRIORITY" ? "🔥" : "⚡";
  const statusEmoji = q.status === "QUALIFIED" ? "✅" : "🔍";

  const signalItems = githubSignals.length
    ? githubSignals.slice(0, 8).map(s => "`" + s + "`").join(" · ")
    : "none";

  const profileSignalItems = profileSignals.length
    ? profileSignals.slice(0, 8).map(s => "`" + s + "`").join(" · ")
    : "none";

  const lines = [];
  lines.push(actionEmoji + " **ACTION: " + actionWord + "**");
  lines.push("");
  lines.push("**👤 PERSON**");
  lines.push("**Name:** [" + actorLogin + "](https://github.com/" + actorLogin + ")");
  lines.push("**Email:** " + (email || "not found"));
  lines.push("**Profile Links:** " + (allProfileLinks.length ? allProfileLinks.join(", ") : "none"));
  lines.push("**Website / Portfolio:** " + (website || "not found"));
  lines.push("");
  lines.push("**📦 GITHUB ACTIVITY**");
  lines.push("**Repository:** [" + fullName + "](" + url + ")");
  lines.push("**Action:** " + actionVerb);
  lines.push("");
  lines.push("**📡 SIGNALS**");
  lines.push("**GitHub:** " + signalItems);
  lines.push("**Profile:** " + profileSignalItems);
  lines.push("**Website:** " + (website || "not found"));
  lines.push("");
  lines.push("**🏆 QUALIFICATION**");
  lines.push("**Score:** **" + q.score + "/100**");
  lines.push("**Ranking:** **" + q.ranking + "** " + rankingEmoji);
  lines.push("**Reason:** " + reason);
  lines.push("");
  lines.push("**⏭️ NEXT ACTION**");
  lines.push("**Status:** " + statusEmoji + " " + q.status);
  lines.push("**Action:** " + q.nextAction);
  lines.push("");

  return { content: lines.join("\n"), key: item.key, data: { score: q.score, ranking: q.ranking } };
}

// -- HTTP handler --
const server = createServer(async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") { res.writeHead(204); return res.end(); }
  if (req.method !== "POST") {
    res.writeHead(405, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ error: "Method not allowed -- POST only" }));
  }

  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  let body;
  try {
    body = JSON.parse(Buffer.concat(chunks).toString() || "{}");
  } catch {
    res.writeHead(400, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ error: "Invalid JSON body" }));
  }

  const items = body.items || [];
  if (items.length === 0) {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, posted: 0, reason: "No items in payload" }));
  }

  const results = [];
  for (const item of items) {
    try {
      const formatted = await formatRepoMessage(item);
      if (!formatted) {
        results.push({ key: item.key, status: "skipped", reason: "not a github_events item" });
        continue;
      }
      if (formatted.skipped) {
        results.push({ key: item.key, status: "skipped", reason: formatted.reason });
        continue;
      }
      const buzzResult = await sendToBuzz(formatted.content);
      results.push({ key: item.key, status: "posted", event_id: buzzResult.event_id, score: formatted.data.score, ranking: formatted.data.ranking });
    } catch (err) {
      results.push({ key: item.key, status: "failed", error: err.message.slice(0, 200) });
    }
  }

  const posted = results.filter(r => r.status === "posted").length;
  const failed = results.filter(r => r.status === "failed").length;
  const skipped = results.filter(r => r.status === "skipped").length;

  console.log("[" + new Date().toISOString() + "] GitHub -> Buzz: " + posted + " posted, " + skipped + " skipped, " + failed + " failed");
  res.writeHead(failed > 0 && posted === 0 ? 502 : 200, { "Content-Type": "application/json" });
  return res.end(JSON.stringify({ ok: failed === 0, posted, skipped, failed, results }));
});

server.listen(PORT, () => {
  console.log("GitHub -> Buzz Bridge v2 (Social Listener) listening on http://localhost:" + PORT);
  console.log("   Channel: * | GitHubs (" + GITHUBS_CHANNEL_ID + ")");
  console.log("   GitHub token: " + (GITHUB_TOKEN ? "configured" : "NOT SET (rate-limited enrichment)"));
  console.log("   Engine: " + (ENGINE_API_KEY ? ENGINE_MODEL : "no API key (templated reason)"));
  console.log("   Relay: " + BUZZ_RELAY_URL);
  console.log("   Endpoint: POST http://localhost:" + PORT + "/hook");
});

process.on("SIGINT", () => { console.log("\nShutting down..."); server.close(); process.exit(0); });
process.on("SIGTERM", () => { server.close(); process.exit(0); });
