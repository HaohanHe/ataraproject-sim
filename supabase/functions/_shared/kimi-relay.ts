// Temporary Kimi relay for local development (owner decision 2026-10-05).
//
// OpenAI-compatible: POST /v1/chat/completions (streaming passes through) and GET /v1/models.
// Callers authenticate with their personal API token (s26_…); the database (kimi_relay_begin)
// applies the token rules, the relay switch, leaderboard eligibility and the per-team daily and
// concurrency limits, and records counters only (never prompt or answer content). The organizer's
// Kimi key is a function secret and never leaves this function.

export const NOTICE_ZH =
  "这是组委会临时提供的 Kimi 额度，方便大家本地开发调试，额度有限，可能随时调整或结束。正式评测和决赛会使用各队在「密钥与网络」里保存的模型服务，记得提前配置好哦。";
export const NOTICE_EN =
  "This is a temporary Kimi allowance from the organizers to help with local development and debugging. It's limited and may change or end at any time. Platform evaluations and the final use the model service each team saves in 'Keys and network' — please make sure yours is set up.";

const EXHAUSTED = "今天的临时 Kimi 额度用完啦，明天会恢复；也可以先用自己的模型服务。 / " +
  "Today's temporary Kimi allowance is used up; it resets tomorrow. You can also use your own model service in the meantime.";

/** Error code → [HTTP status, message shown to the contestant]. */
export const ERRORS: Record<string, [number, string]> = {
  invalid_token: [401, "个人 API 令牌无效（在个人资料页创建 s26_… 令牌） / Invalid personal API token (create an s26_… token on your profile page)."],
  cli_tokens_disabled: [403, "个人 API 令牌暂未开放 / Personal API tokens are not available right now."],
  account_banned: [403, "该账号已被停用 / This account is suspended."],
  rate_limited: [429, "请求太频繁，请稍后再试 / Too many requests, please slow down."],
  relay_disabled: [503, "临时 Kimi 中转目前已关闭，请使用自己的模型服务 / The temporary Kimi relay is currently off; please use your own model service. " + NOTICE_EN],
  team_required: [403, "请先加入队伍 / Join a team first."],
  not_eligible: [403, "上榜后即可使用（正式赛有一次成功评测） / Available once your team is on the leaderboard (one scored formal evaluation in the online phase)."],
  daily_requests_exhausted: [429, EXHAUSTED],
  daily_tokens_exhausted: [429, EXHAUSTED],
  upstream_quota: [429, "临时 Kimi 额度暂时用完啦，稍后会恢复；也可以先用自己的模型服务。 / The temporary Kimi allowance is used up for now; it will come back later. You can also use your own model service in the meantime."],
  too_many_concurrent: [429, "本队同时进行的请求已达上限，请等前一个请求结束 / Too many concurrent requests for your team; wait for one to finish."],
  evaluation_not_allowed: [403, "临时 Kimi 中转仅用于本地开发调试，不能用于平台评测 / The temporary Kimi relay is for local development only, not for platform evaluations. " + NOTICE_EN],
  invalid_request: [400, "请求格式不正确（需要 OpenAI chat/completions JSON） / Invalid request (OpenAI chat/completions JSON expected)."],
  request_too_large: [413, "请求太大 / Request too large."],
  not_found: [404, "仅支持 POST /v1/chat/completions 和 GET /v1/models / Only POST /v1/chat/completions and GET /v1/models."],
  relay_unavailable: [503, "临时 Kimi 中转暂时不可用，请稍后再试 / The temporary Kimi relay is unavailable; try again later."],
};

export class RelayError extends Error {
  constructor(public code: string) {
    super(code);
  }
}

export function errorResponse(code: string, headers: Record<string, string> = {}): Response {
  const [status, message] = ERRORS[code] ?? ERRORS.relay_unavailable;
  return Response.json({ error: { message, type: "kimi_relay_error", code: ERRORS[code] ? code : "relay_unavailable" } }, {
    status,
    headers,
  });
}

export const BODY_LIMIT = 4 * 1024 * 1024;

/** The request forwarded upstream: fixed model, clamped output length, usage reported when streaming. */
export function prepareBody(body: Record<string, unknown>, model: string, maxTokens: number): Record<string, unknown> {
  if (!Array.isArray(body.messages) || !body.messages.length) throw new RelayError("invalid_request");
  const out: Record<string, unknown> = { ...body, model };
  for (const key of ["max_tokens", "max_completion_tokens"]) {
    const v = out[key];
    if (v !== undefined && v !== null) {
      if (typeof v !== "number" || !Number.isFinite(v) || v < 1) throw new RelayError("invalid_request");
      out[key] = Math.min(Math.floor(v), maxTokens);
    }
  }
  if (out.max_tokens == null && out.max_completion_tokens == null) out.max_tokens = maxTokens;
  if (typeof out.n === "number" && out.n > 1) out.n = 1;
  if (out.stream === true) {
    const opts = out.stream_options && typeof out.stream_options === "object" ? out.stream_options : {};
    out.stream_options = { ...(opts as Record<string, unknown>), include_usage: true };
  } else if (out.stream !== undefined && out.stream !== false) {
    throw new RelayError("invalid_request");
  }
  return out;
}

export type Usage = { prompt: number; completion: number; total: number };

export function readUsage(value: unknown): Usage | null {
  const u = (value as { usage?: Record<string, unknown> } | null)?.usage;
  if (!u || typeof u !== "object") return null;
  const n = (x: unknown) => (typeof x === "number" && Number.isFinite(x) ? Math.max(0, Math.floor(x)) : 0);
  const prompt = n(u.prompt_tokens), completion = n(u.completion_tokens);
  return { prompt, completion, total: n(u.total_tokens) || prompt + completion };
}

/** How long a pool key rests after an upstream error (0 = not a key problem, no retry):
 * weekly/plan limits, termination and auth errors 24 h; a plain rate limit a few minutes. */
export function keyCooldownSeconds(status: number, detail: string): number {
  const d = detail.toLowerCase();
  const long = /weekly|7-day|access_terminated|quota|insufficient|billing|exceeded_current|suspend/.test(d);
  if (status === 401 || status === 402 || status === 403) return 24 * 3600;
  if (status === 429) return long ? 24 * 3600 : 300;
  return long && status < 500 ? 24 * 3600 : 0;
}

/** Scans an SSE stream for the last `usage` object while the bytes pass through unchanged. */
export class SseUsage {
  private buffer = "";
  private decoder = new TextDecoder();
  usage: Usage | null = null;
  bytes = 0;
  push(chunk: Uint8Array) {
    this.bytes += chunk.byteLength;
    this.buffer += this.decoder.decode(chunk, { stream: true });
    let i: number;
    while ((i = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, i).trim();
      this.buffer = this.buffer.slice(i + 1);
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]" || !data.includes('"usage"')) continue;
      try {
        this.usage = readUsage(JSON.parse(data)) ?? this.usage;
      } catch { /* not JSON: ignore */ }
    }
    if (this.buffer.length > 1 << 20) this.buffer = "";
  }
}
