/**
 * Wire types, mirroring the gateway's pydantic schemas field-for-field.
 *
 * Sources, in order: `app/schemas/errors.py`, `app/schemas/auth.py`,
 * `app/schemas/conversations.py`, `app/schemas/chat.py`. When one of those
 * changes, this file changes with it — there is no code generation, so this is
 * the one place the frontend restates the contract.
 *
 * Slot names are `string`, never a union. Slots are defined in
 * `config/providers.yaml` and adding one is a config edit; a TypeScript enum
 * would go stale the moment someone adds a slot, and the whole point of the slot
 * indirection is that a client never hardcodes model identity.
 */

// --------------------------------------------------------------------------- //
// The error envelope — app/schemas/errors.py
// --------------------------------------------------------------------------- //
export type ErrorBody = {
  /** Stable, machine-readable, snake_case. Branch on this, never on `message`. */
  code: string;
  /** Human-readable and safe to display verbatim. */
  message: string;
  /** Echoed in `X-Request-ID` and bound to every log line for the call. */
  request_id: string | null;
  details: Record<string, unknown>;
};

export type ErrorResponse = { error: ErrorBody };

// --------------------------------------------------------------------------- //
// Identity — app/schemas/auth.py
// --------------------------------------------------------------------------- //
export type Me = {
  user_id: string;
  email: string;
  email_verified: boolean;
  tier: string;
  auth_method: "session" | "api_key";
  api_key_id: string | null;
};

// --------------------------------------------------------------------------- //
// Canonical content blocks — app/memory/canonical.py (Contract B)
// --------------------------------------------------------------------------- //
export type TextBlock = { type: "text"; text: string };

export type FileRefBlock = {
  type: "file_ref";
  file_hash: string;
  filename: string;
  mime: string;
  bytes: number;
};

export type OmissionMarkerBlock = {
  type: "omission_marker";
  omitted_count: number;
  reason: "context_truncation";
};

/**
 * The open end of the union is deliberate. `tool_call`, `tool_result` and
 * `summary` are reserved block types that a later phase will start writing, and
 * a transcript that throws on an unrecognised block would turn a forward-compatible
 * schema into a broken page. The renderer falls through to a neutral notice.
 */
export type UnknownBlock = { type: string; [key: string]: unknown };

export type ContentBlock = TextBlock | FileRefBlock | OmissionMarkerBlock | UnknownBlock;

/**
 * Provenance as stored — `MessageMeta.to_jsonb()` writes every key, including
 * nulls and defaults, so a reader never has to guess whether a missing key means
 * "false" or "not recorded". Typed accordingly: present, possibly null.
 */
export type MessageMeta = {
  provider_used: string | null;
  model_used: string | null;
  slot_used: string | null;
  requested_slot: string | null;
  substituted: boolean;
  attempts: number;
  tokens_in: number | null;
  tokens_out: number | null;
  wasted_tokens_out: number;
  degraded: boolean;
  extraction_tier: "cache" | "native" | "llm" | "local" | null;
};

export type MessageRole = "system" | "user" | "assistant";

export type Message = {
  id: string;
  /** The ordering key. Not `created_at` — two rows can share a millisecond. */
  seq: number;
  role: MessageRole;
  content: ContentBlock[];
  meta: Partial<MessageMeta>;
  created_at: string;
};

// --------------------------------------------------------------------------- //
// Conversations — app/schemas/conversations.py
// --------------------------------------------------------------------------- //
export type Conversation = {
  id: string;
  title: string | null;
  preferred_slot: string;
  /** D3 seam: set by a conversation's first tool call. Always null in v1. */
  pinned_model: string | null;
  created_at: string;
  /** Bumped on every turn. The sidebar orders on this. */
  updated_at: string;
};

export type ConversationDetail = Conversation & { messages: Message[] };

// --------------------------------------------------------------------------- //
// Chat — app/schemas/chat.py
// --------------------------------------------------------------------------- //
export type InputMessage = {
  /** `assistant` is absent by design: the gateway owns history, clients cannot forge it. */
  role: "system" | "user";
  content: string;
  /** Hashes from `POST /v1/files`, at most 4. A hash this user does not own is a 404. */
  file_refs?: string[];
};

export type ChatCompletionRequest = {
  model: string;
  messages: InputMessage[];
  conversation_id?: string;
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  stop?: string[];
  stream?: boolean;
};

/** Which model actually answered. The `ModelIndicator` reads this. */
export type ServedBy = { slot: string; provider: string; model: string };

export type UsageOut = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  /** True when the provider reported nothing and these are the gateway's own numbers. */
  estimated: boolean;
};

export type ChatCompletionResponse = {
  /** The gateway's request_id — same value as `X-Request-ID`. */
  id: string;
  object: "chat.completion";
  created: number;
  model: string;
  choices: { index: number; message: { role: "assistant"; content: string }; finish_reason: string }[];
  usage: UsageOut;

  // The §1.1 provenance fields, present on every response since the first one.
  served_by: ServedBy;
  requested_slot: string;
  substituted: boolean;
  attempts: number;
  degraded: boolean;

  conversation_id: string;
  message_id: string;
};

// --------------------------------------------------------------------------- //
// Live model status — app/schemas/models.py (D21)
// --------------------------------------------------------------------------- //
export type SlotStatus = "available" | "rate_limited" | "unavailable" | "unknown";

export type BreakerStateOut = "closed" | "open" | "half_open";

export type WindowStatus = {
  window: "rpm" | "rpd" | "tpm" | "tpd";
  limit: number;
  remaining: number;
  resets_at: string;
};

export type CandidateStatus = {
  provider: string;
  model: string;
  status: SlotStatus;
  breaker_state: BreakerStateOut;
  /** When this candidate is expected to become available again. `null` when
   *  `status` is `available` or `unknown` — there is nothing to wait out. */
  resets_at: string | null;
  windows: WindowStatus[];
};

/** One logical slot — `auto` or a named slot from `providers.yaml`. */
export type ModelEntry = {
  id: string;
  object: "model";
  created: number;
  /** The primary candidate's provider. `null` for `auto`, which has no single owner. */
  owned_by: string | null;
  status: SlotStatus;
  resets_at: string | null;
  description: string;
  candidates: CandidateStatus[];
};

/** `GET /v1/models`'s body: OpenAI's list envelope, plus everything the gateway knows. */
export type ModelsResponse = {
  object: "list";
  data: ModelEntry[];
};
