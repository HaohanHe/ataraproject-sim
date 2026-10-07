import { assertEquals, assertThrows } from "@std/assert";
import { keyCooldownSeconds, prepareBody, readUsage, RelayError, SseUsage } from "./kimi-relay.ts";

Deno.test("prepareBody fixes the model, clamps max_tokens and asks for streamed usage", () => {
  const out = prepareBody({ model: "x", messages: [{ role: "user", content: "hi" }], max_tokens: 99999, stream: true }, "kimi-for-coding", 8192);
  assertEquals(out.model, "kimi-for-coding");
  assertEquals(out.max_tokens, 8192);
  assertEquals(out.stream_options, { include_usage: true });
  assertEquals(prepareBody({ messages: [{ role: "user", content: "hi" }] }, "m", 8192).max_tokens, 8192);
  assertThrows(() => prepareBody({ messages: [] }, "m", 8192), RelayError);
});

Deno.test("usage is read from JSON bodies and SSE streams", () => {
  assertEquals(readUsage({ usage: { prompt_tokens: 3, completion_tokens: 4 } }), { prompt: 3, completion: 4, total: 7 });
  const scan = new SseUsage();
  const enc = new TextEncoder();
  scan.push(enc.encode('data: {"choices":[]}\n\ndata: {"choices":[],"usa'));
  scan.push(enc.encode('ge":{"prompt_tokens":5,"completion_tokens":6,"total_tokens":11}}\n\ndata: [DONE]\n\n'));
  assertEquals(scan.usage, { prompt: 5, completion: 6, total: 11 });
});

Deno.test("key cooldowns: weekly limit and auth long, rate limit short, server errors none", () => {
  assertEquals(keyCooldownSeconds(403, "access_terminated_error You've reached your weekly (7-day) usage limit"), 86400);
  assertEquals(keyCooldownSeconds(401, ""), 86400);
  assertEquals(keyCooldownSeconds(429, "rate_limit_reached_error"), 300);
  assertEquals(keyCooldownSeconds(429, "weekly usage limit"), 86400);
  assertEquals(keyCooldownSeconds(500, ""), 0);
  assertEquals(keyCooldownSeconds(400, "invalid_request_error"), 0);
});
