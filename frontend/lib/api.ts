"use client";

import { getSupabaseBrowserClient } from "./supabase/client";
import type {
  ChatCompletionRequest,
  ChatCompletionResponse,
  Conversation,
  ConversationDetail,
  ErrorResponse,
  FileUploadResponse,
  Me,
  MessagePage,
  ModelsResponse,
  ProviderKeyStatus,
} from "./types";

/** Same-origin prefix; `next.config.ts` rewrites it to the gateway. */
export const GATEWAY_BASE = "/api/gw";

/** Also the SWR cache key for the settings list — see `useProviderKeys`. */
export const PROVIDER_KEYS_KEY = "/v1/provider-keys";

/**
 * The URL of one older page of a thread's history (D48).
 *
 * Deliberately **not** an SWR key. The head of a thread lives at
 * `conversationKey(id)` and is what an optimistic turn mutates; older pages are
 * immutable history held in component state by `useConversation`. Giving them a
 * cache entry under a sibling of the head key is the exact mistake two routes
 * exist to prevent — a `globalMutate` of the thread would then have two
 * entries to reconcile, and the one holding page four would win somewhere.
 */
export function conversationMessagesKey(id: string, beforeSeq: number | null): string {
  const query = beforeSeq === null ? "" : `?before_seq=${beforeSeq}`;
  return `/v1/conversations/${id}/messages${query}`;
}

/**
 * A gateway failure, carrying the error envelope intact.
 *
 * `requestId` is the reason this class exists rather than a bare `Error`: the
 * envelope carries it on every failure specifically so a user can quote it, and
 * a UI that throws it away turns a traceable incident back into "it broke".
 */
export class GatewayError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: Record<string, unknown>;
  /** From the `Retry-After` header, in seconds. `null` when the response didn't
   *  carry one — most failures don't. D17's all-quota-skipped path and, once
   *  Step 10 lands, our own rate limit both set it on a `rate_limited` response. */
  readonly retryAfterS: number | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    requestId: string | null;
    details?: Record<string, unknown>;
    retryAfterS?: number | null;
  }) {
    super(init.message);
    this.name = "GatewayError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.details = init.details ?? {};
    this.retryAfterS = init.retryAfterS ?? null;
  }

  get isNotFound() {
    return this.status === 404;
  }

  get isUnauthenticated() {
    return this.status === 401;
  }

  /** `code === "rate_limited"` is a wait, not a failure — the caller should
   *  render it as such rather than as an ordinary error. */
  get isRateLimited() {
    return this.code === "rate_limited";
  }
}

/** Transport failure — the gateway was never reached. Distinct on purpose: the
 *  user-facing wording for "we can't reach the service" is not the wording for
 *  "the service said no". */
export class NetworkError extends Error {
  constructor(message = "Could not reach the gateway.") {
    super(message);
    this.name = "NetworkError";
  }
}

/**
 * The `Authorization` header, or nothing when there is no session.
 *
 * Exported because `sse.ts` needs the identical header on its own `fetch` —
 * `EventSource` cannot set one, which is the reason the streaming client is
 * hand-rolled at all. Two copies of this would be two places to get a token
 * refresh wrong.
 */
export async function authHeaders(): Promise<Record<string, string>> {
  const supabase = getSupabaseBrowserClient();
  // `getSession` refreshes an expired access token if the refresh token is
  // still good, so this is also the retry path for a token that aged out while
  // a tab sat open.
  const {
    data: { session },
  } = await supabase.auth.getSession();

  return session?.access_token ? { Authorization: `Bearer ${session.access_token}` } : {};
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== "object" || value === null) return false;
  const error = (value as { error?: unknown }).error;
  return typeof error === "object" && error !== null && "code" in error && "message" in error;
}

/**
 * A non-2xx response → the `GatewayError` it describes.
 *
 * Every non-2xx from the gateway is the envelope. A body that is not one came
 * from somewhere else — the Next proxy, or a crashed worker — so it is reported
 * as such rather than mangled into a fake code.
 *
 * Exported for the streaming client, and that is D13's payoff on this side of
 * the wire: the gateway does not write a single byte of an SSE body until an
 * upstream attempt has produced its first chunk, so *every* pre-stream failure
 * is still an ordinary error envelope and gets translated right here, by the
 * same function, into the same class the non-streaming path already throws.
 */
export async function gatewayErrorFrom(response: Response): Promise<GatewayError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* non-JSON error body */
  }

  const retryAfterS = parseRetryAfter(response.headers.get("Retry-After"));

  if (isErrorResponse(body)) {
    return new GatewayError({
      status: response.status,
      code: body.error.code,
      message: body.error.message,
      requestId: body.error.request_id ?? response.headers.get("X-Request-ID"),
      details: body.error.details,
      retryAfterS,
    });
  }

  return new GatewayError({
    status: response.status,
    code: "unexpected_response",
    message: `The gateway returned an unexpected ${response.status} response.`,
    requestId: response.headers.get("X-Request-ID"),
    retryAfterS,
  });
}

/** `Retry-After` is always delta-seconds on this gateway, never an HTTP-date. */
function parseRetryAfter(header: string | null): number | null {
  if (header === null) return null;
  const seconds = Number(header);
  return Number.isFinite(seconds) ? seconds : null;
}

async function request<T>(
  path: string,
  init: RequestInit & { parse?: boolean } = {},
): Promise<T> {
  const { parse = true, ...rest } = init;

  let response: Response;
  try {
    response = await fetch(`${GATEWAY_BASE}${path}`, {
      ...rest,
      headers: {
        // Only for a JSON body. A `FormData` body must be allowed to set its
        // own `Content-Type`, because the boundary token is generated with it
        // — declaring `multipart/form-data` by hand produces a header with no
        // boundary, and the gateway's parser rejects it as malformed before a
        // single byte of the file is read.
        ...(typeof rest.body === "string" ? { "Content-Type": "application/json" } : {}),
        ...(await authHeaders()),
        ...rest.headers,
      },
    });
  } catch {
    throw new NetworkError();
  }

  if (!response.ok) throw await gatewayErrorFrom(response);

  if (!parse || response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  me: () => request<Me>("/v1/me"),

  /** Live per-slot status, no upstream call (D21). `useModels` is the usual caller. */
  fetchModels: () => request<ModelsResponse>("/v1/models"),

  /**
   * Upload one file and get back the hash a turn can reference.
   *
   * The only thing on this side of the wire that puts bytes into the gateway.
   * Called on *selection* rather than on send (Step 10), so the hash is already
   * in hand by the time the message is written — an upload that starts when the
   * user hits Enter would put a multi-second pause between the send and the
   * first token, on top of the extraction the first turn already pays for.
   *
   * A 413 or a 415 arrives as an ordinary `GatewayError` and is rendered as
   * itself; the client-side gate in `lib/files.ts` is what keeps the common
   * cases from getting this far.
   */
  uploadFile: (file: File, signal?: AbortSignal) => {
    const form = new FormData();
    // `"file"` is `files.UPLOAD_FIELD`. The gateway parses the multipart body
    // itself and keeps exactly this one part.
    form.append("file", file, file.name);
    return request<FileUploadResponse>("/v1/files", { method: "POST", body: form, signal });
  },

  listConversations: () => request<Conversation[]>("/v1/conversations"),

  getConversation: (id: string) => request<ConversationDetail>(`/v1/conversations/${id}`),

  /**
   * One page of history older than `beforeSeq` — the scroll-up fetch.
   *
   * `beforeSeq` comes from the previous response's `next_before_seq` and never
   * from arithmetic on a `seq` that happens to be on screen: `seq` is gap-free
   * per conversation today, but the cursor is the server's to define, and
   * inventing one here would silently skip a message the day that changes.
   */
  fetchMessagePage: (id: string, beforeSeq: number | null) =>
    request<MessagePage>(conversationMessagesKey(id, beforeSeq)),

  renameConversation: (id: string, title: string | null) =>
    request<Conversation>(`/v1/conversations/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  deleteConversation: (id: string) =>
    request<void>(`/v1/conversations/${id}`, { method: "DELETE", parse: false }),

  /**
   * The non-streaming turn.
   *
   * Kept working, and deliberately not reached by the UI since Step 11 — the
   * chat always streams. It is the fallback for a client that cannot read SSE,
   * and it is the shape Phase 3's cache-hit replay answers with, so letting it
   * rot would cost more than the one function it takes to keep.
   */
  createCompletion: (body: ChatCompletionRequest) =>
    request<ChatCompletionResponse>("/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ------------------------------------------------------------------------ //
  // BYOK provider keys (Phase 6, §9.2)
  // ------------------------------------------------------------------------ //
  /** One row per *enabled* provider, key or no key — an empty settings page is
   *  still a full list, and the client never hardcodes the provider set. */
  listProviderKeys: () => request<ProviderKeyStatus[]>(PROVIDER_KEYS_KEY),

  /**
   * Add a key. The gateway validates it against the provider **before** storing
   * anything (§9.2), so the two failures a caller has to tell apart both arrive
   * here as a `GatewayError` and must not be flattened together:
   * `invalid_provider_key` (422 — the provider rejected it) and
   * `provider_unavailable` (503 — we could not check). A 429 is D43's
   * anti-abuse floor on this route, five an hour.
   */
  addProviderKey: (provider: string, key: string, nickname?: string | null) =>
    request<ProviderKeyStatus>(PROVIDER_KEYS_KEY, {
      method: "POST",
      body: JSON.stringify({ provider, key, ...(nickname ? { nickname } : {}) }),
    }),

  /** Soft-deletes the row. Deliberately leaves the user's `q:{user_id}:…`
   *  counters alone — they record what that key really spent (trap 4). */
  removeProviderKey: (provider: string) =>
    request<void>(`${PROVIDER_KEYS_KEY}/${provider}`, { method: "DELETE", parse: false }),

  /** The settings page's "check again" — `validation_status` is a snapshot, and
   *  this is the only thing that refreshes it short of a real request failing. */
  revalidateProviderKey: (provider: string) =>
    request<ProviderKeyStatus>(`${PROVIDER_KEYS_KEY}/${provider}/validate`, { method: "POST" }),
};

/** SWR's fetcher: the key is the path, so cache keys read as URLs in devtools. */
export const swrFetcher = <T>(path: string): Promise<T> => request<T>(path);
