// kimi-relay — temporary OpenAI-compatible Kimi relay for contestants' LOCAL development.
// See _shared/kimi-relay.ts and supabase/migrations/20261005090000_kimi_relay.sql.
// Secrets: the key pool KIMI_RELAY_KEY, KIMI_RELAY_KEY_2 … KIMI_RELAY_KEY_9 (slots 1…9; never logged,
// stored or returned — the database only knows slot numbers), optional KIMI_RELAY_BASE / KIMI_RELAY_MODEL.
import { createClient } from "npm:@supabase/supabase-js@2";
import { sha256Hex } from "../_shared/cli-gateway.ts";
import {
  BODY_LIMIT,
  errorResponse,
  prepareBody,
  readUsage,
  RelayError,
  SseUsage,
  keyCooldownSeconds,
  type Usage,
} from "../_shared/kimi-relay.ts";

const KEYS = new Map<number, string>();
for (let slot = 1; slot <= 9; slot++) {
  const key = Deno.env.get(slot === 1 ? "KIMI_RELAY_KEY" : "KIMI_RELAY_KEY_" + slot);
  if (key) KEYS.set(slot, key);
}
const SLOTS = [...KEYS.keys()];
const BASE = (Deno.env.get("KIMI_RELAY_BASE") ?? "https://api.kimi.com/coding/v1").replace(/\/+$/, "");
const MODEL = Deno.env.get("KIMI_RELAY_MODEL") ?? "kimi-for-coding";
const UPSTREAM_TIMEOUT_MS = 600_000;
const service = createClient(Deno.env.get("SUPABASE_URL") ?? "", Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "", {
  auth: { persistSession: false },
});
const cors: Record<string, string> = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "cache-control": "no-store",
};
const DB_CODES = new Set([
  "invalid_token",
  "cli_tokens_disabled",
  "account_banned",
  "rate_limited",
  "relay_disabled",
  "team_required",
  "not_eligible",
  "daily_requests_exhausted",
  "daily_tokens_exhausted",
  "too_many_concurrent",
]);

function tokenHashOf(request: Request): Promise<string> {
  // Accept the OpenAI SDK's Authorization header and the Anthropic-style x-api-key.
  const raw = /^Bearer\s+(\S+)$/i.exec(request.headers.get("authorization") ?? "")?.[1] ??
    request.headers.get("x-api-key") ?? "";
  if (!/^s26_[0-9a-f]{64}$/.test(raw)) throw new RelayError("invalid_token");
  return sha256Hex(raw);
}

async function rpc<T>(fn: string, args: Record<string, unknown>): Promise<T> {
  const { data, error } = await service.rpc(fn, args);
  if (error) throw new RelayError(DB_CODES.has(error.message) ? error.message : "relay_unavailable");
  return data as T;
}

async function finish(id: number, status: string, http: number | null, usage: Usage | null) {
  const { error } = await service.rpc("kimi_relay_finish", {
    p_id: id,
    p_status: status,
    p_http: http,
    p_prompt: usage?.prompt ?? 0,
    p_completion: usage?.completion ?? 0,
    p_total: usage?.total ?? 0,
  });
  if (error) console.error("kimi-relay finish", error.message);
}

async function readBody(request: Request): Promise<Record<string, unknown>> {
  if (Number(request.headers.get("content-length") ?? "0") > BODY_LIMIT) throw new RelayError("request_too_large");
  const text = await request.text();
  if (text.length > BODY_LIMIT) throw new RelayError("request_too_large");
  try {
    const body = JSON.parse(text);
    if (body && typeof body === "object" && !Array.isArray(body)) return body;
  } catch { /* fall through */ }
  throw new RelayError("invalid_request");
}

async function chat(request: Request): Promise<Response> {
  const hash = await tokenHashOf(request);
  const body = await readBody(request);
  const stream = body.stream === true;
  const begun = await rpc<{ usage_id: number; max_tokens: number; block_edge_runtime: boolean }>(
    "kimi_relay_begin",
    { p_hash: hash, p_stream: stream },
  );
  const id = Number(begun.usage_id);
  let handedOff = false;
  try {
    // The platform's own model proxy (observer-model) runs on the Deno edge runtime; a team that
    // saved this relay as its evaluation model would reach us from there. Local tools do not.
    if (begun.block_edge_runtime && /^Deno\//.test(request.headers.get("user-agent") ?? "")) {
      await finish(id, "refused_evaluation", 403, null);
      handedOff = true;
      return errorResponse("evaluation_not_allowed", cors);
    }
    let forwarded: Record<string, unknown>;
    try {
      forwarded = prepareBody(body, MODEL, Number(begun.max_tokens) || 8192);
    } catch (e) {
      await finish(id, "invalid_request", 400, null);
      handedOff = true;
      throw e;
    }
    // Least recently used healthy key; a key-level error (limit, quota, auth) cools that key
    // down and the request is retried once on another key.
    const tried: number[] = [];
    let upstream: Response | null = null;
    for (let attempt = 0; attempt < 2 && !upstream; attempt++) {
      const slot = await rpc<number | null>("kimi_relay_pick_key", { p_usage: id, p_slots: SLOTS, p_exclude: tried });
      if (!slot || !KEYS.has(slot)) break;
      tried.push(slot);
      const response = await fetch(BASE + "/chat/completions", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "authorization": "Bearer " + KEYS.get(slot),
          "accept": stream ? "text/event-stream" : "application/json",
        },
        body: JSON.stringify(forwarded),
        signal: AbortSignal.any([request.signal, AbortSignal.timeout(UPSTREAM_TIMEOUT_MS)]),
      });
      if (response.ok) {
        upstream = response;
        break;
      }
      // Provider error bodies stay here; only the status and the error type are recorded.
      const text = await response.text().catch(() => "");
      let type = "";
      try {
        type = String(JSON.parse(text)?.error?.type ?? "");
      } catch { /* not JSON */ }
      const cool = keyCooldownSeconds(response.status, type + " " + text.slice(0, 400));
      await service.rpc("kimi_relay_key_failed", {
        p_slot: slot,
        p_status: response.status,
        p_error: type || "http_" + response.status,
        p_cool_seconds: cool,
      });
      if (cool === 0) {
        await finish(id, "upstream_" + response.status, response.status, null);
        handedOff = true;
        if (response.status === 400) return errorResponse("invalid_request", cors);
        return errorResponse("relay_unavailable", cors);
      }
    }
    if (!upstream) {
      await finish(id, tried.length ? "upstream_quota" : "no_key", null, null);
      handedOff = true;
      return errorResponse("upstream_quota", cors);
    }
    if (!stream || !upstream.body) {
      const payload = await upstream.json().catch(() => null);
      await finish(id, payload ? "ok" : "failed", upstream.status, readUsage(payload));
      handedOff = true;
      if (!payload) return errorResponse("relay_unavailable", cors);
      return Response.json(payload, { headers: cors });
    }
    // Streaming: bytes pass through unchanged; the usage chunk is read on the way.
    const scan = new SseUsage();
    const reader = upstream.body.getReader();
    let done = false;
    const out = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const next = await reader.read();
          if (next.done) {
            done = true;
            controller.close();
            await finish(id, "ok", 200, scan.usage ?? estimate(scan.bytes));
            return;
          }
          scan.push(next.value);
          controller.enqueue(next.value);
        } catch (e) {
          if (!done) {
            done = true;
            await finish(id, "failed", 200, scan.usage ?? estimate(scan.bytes));
          }
          controller.error(e);
        }
      },
      async cancel() {
        await reader.cancel().catch(() => {});
        if (!done) {
          done = true;
          await finish(id, "client_closed", 200, scan.usage ?? estimate(scan.bytes));
        }
      },
    });
    handedOff = true;
    return new Response(out, {
      headers: {
        ...cors,
        "content-type": upstream.headers.get("content-type") ?? "text/event-stream",
        "x-accel-buffering": "no",
      },
    });
  } catch (e) {
    if (!handedOff) await finish(id, "failed", null, null);
    throw e;
  }
}

/** Without a usage chunk (client gone early): roughly one output token per 40 bytes of SSE. */
function estimate(bytes: number): Usage {
  const completion = Math.ceil(bytes / 40);
  return { prompt: 0, completion, total: completion };
}

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  const path = new URL(request.url).pathname.replace(/\/+$/, "");
  try {
    if (!KEYS.size) throw new RelayError("relay_unavailable");
    if (request.method === "POST" && path.endsWith("/chat/completions")) return await chat(request);
    if (request.method === "GET" && path.endsWith("/models")) {
      await rpc("kimi_relay_check", { p_hash: await tokenHashOf(request) });
      return Response.json({ object: "list", data: [{ id: MODEL, object: "model", owned_by: "kimi" }] }, {
        headers: cors,
      });
    }
    throw new RelayError("not_found");
  } catch (error) {
    if (!(error instanceof RelayError)) console.error("kimi-relay", error instanceof Error ? error.name : "error");
    return errorResponse(error instanceof RelayError ? error.code : "relay_unavailable", cors);
  }
});
