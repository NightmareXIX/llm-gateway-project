"use client";

import { getSupabaseBrowserClient } from "./supabase/client";
import type {
  ChatCompletionRequest,
  ChatCompletionResponse,
  Conversation,
  ConversationDetail,
  ErrorResponse,
  Me,
} from "./types";

/** Same-origin prefix; `next.config.ts` rewrites it to the gateway. */
export const GATEWAY_BASE = "/api/gw";

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

  constructor(init: {
    status: number;
    code: string;
    message: string;
    requestId: string | null;
    details?: Record<string, unknown>;
  }) {
    super(init.message);
    this.name = "GatewayError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.details = init.details ?? {};
  }

  get isNotFound() {
    return this.status === 404;
  }

  get isUnauthenticated() {
    return this.status === 401;
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

  if (isErrorResponse(body)) {
    return new GatewayError({
      status: response.status,
      code: body.error.code,
      message: body.error.message,
      requestId: body.error.request_id ?? response.headers.get("X-Request-ID"),
      details: body.error.details,
    });
  }

  return new GatewayError({
    status: response.status,
    code: "unexpected_response",
    message: `The gateway returned an unexpected ${response.status} response.`,
    requestId: response.headers.get("X-Request-ID"),
  });
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
        ...(rest.body ? { "Content-Type": "application/json" } : {}),
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

  listConversations: () => request<Conversation[]>("/v1/conversations"),

  getConversation: (id: string) => request<ConversationDetail>(`/v1/conversations/${id}`),

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
};

/** SWR's fetcher: the key is the path, so cache keys read as URLs in devtools. */
export const swrFetcher = <T>(path: string): Promise<T> => request<T>(path);
