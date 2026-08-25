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
/** How an attachment reached the model (Phase 4, D25). Mirrors the gateway's
 * own `ExtractionTier`, and the order here is the order the lane tries them. */
export type ExtractionTier = "cache" | "native" | "llm" | "local";

/** Which credential paid for a turn (Phase 6, §9.4). `shared` is the gateway's
 *  own free-tier pool; `private` is a key the user added in Settings. */
export type KeyPool = "shared" | "private";

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
  extraction_tier: ExtractionTier | null;
  /**
   * How many older turns D4's fitting step dropped to build this answer
   * (Phase 5, D34). Absent on a row written before the field existed, and
   * `Partial<MessageMeta>` is how every reader here already takes it — an
   * absent key reads as `0`, which asserts the default rather than asserting
   * that nothing was dropped.
   */
  messages_dropped: number;
  /**
   * Which credential pool served this turn (Phase 6, D42) — `"private"` when
   * the user's own provider key paid for it, `"shared"` when the gateway's
   * pool did. Absent on a row written before the field existed, and `null` on
   * a turn that spent no key at all (a cache hit), exactly as
   * `extraction_tier` is.
   */
  key_pool: KeyPool | null;
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
// Files — app/schemas/files.py (Phase 4)
// --------------------------------------------------------------------------- //
/**
 * One uploaded file, as the gateway describes it back.
 *
 * **The hash is the identifier** — there is no row id here and no URL. The
 * bucket is private (D23), no signed URL is ever generated and there is no
 * download endpoint, so a client can name a file and reference it in a turn but
 * can never fetch the bytes back. That is deliberate, and it is why this type
 * has nothing on it that looks like a link.
 */
export type FileOut = {
  /** SHA-256, lowercase hex. What `InputMessage.file_refs` carries. */
  file_hash: string;
  filename: string;
  /** The **sniffed** type, not the one the upload declared. A PNG named
   *  `report.pdf` comes back as `image/png`, and that is the truth. */
  mime: string;
  bytes: number;
  created_at: string;
};

export type FileUploadResponse = FileOut & {
  /** True when this user already owned this hash: no bytes moved and no row was
   *  written. 201 either way — the endpoint's promise is "this hash is yours to
   *  reference", which was equally true both times. */
  deduplicated: boolean;
};

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
  /** Field-for-field with the `done` event's own, per §1.1. */
  extraction_tier?: ExtractionTier | null;
  /** How many older turns D4's fitting dropped to build this answer (D34).
   *  Always `0` on a cache hit — nothing was rendered that turn. */
  messages_dropped?: number;
  /** D3/D32: set when the conversation is pinned and the pin overrode what
   *  this turn asked for. A disclosure riding on a 200, never an error — it
   *  must not render as a failure (trap 6). */
  warning?: string | null;
  /** D42's eighth disclosure field: which pool's credential served this turn.
   *  `null` on a cache hit — no key was spent, so there is nothing to say. */
  key_pool?: KeyPool | null;

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

// --------------------------------------------------------------------------- //
// BYOK provider keys — app/schemas/keys.py (Phase 6, §9.2/§9.9)
// --------------------------------------------------------------------------- //
/**
 * A key's last verdict from the provider itself.
 *
 * **A snapshot, not a live fact.** `valid` means the provider accepted it the
 * last time anyone asked — at add time, at the user's own re-check, or (for
 * `invalid`) the moment a real request was rejected with it (D40). A key
 * revoked upstream five minutes ago still reads `valid` here until something
 * tries to use it.
 */
export type ProviderKeyValidationStatus = "valid" | "invalid" | "unverified";

/** A stored BYOK credential, as safe to display as it will ever be: the
 *  plaintext is not recoverable from this shape or from any endpoint. */
export type ProviderKeyOut = {
  provider: string;
  /** `"••••a91c"`, built from the last four characters kept at add time. */
  masked: string;
  nickname: string | null;
  validation_status: ProviderKeyValidationStatus;
  last_validated_at: string | null;
  /** When the resolver last handed this credential to a request (D38). */
  last_used_at: string | null;
  is_active: boolean;
  created_at: string;
};

/**
 * One settings row — returned for *every* enabled provider, with or without a
 * stored key, so the client never has to know the provider list to draw an
 * empty "Using shared pool" row.
 */
export type ProviderKeyStatus = {
  provider: string;
  pool: KeyPool;
  /** `null` on the shared-pool rows; present and active on the private ones. */
  key: ProviderKeyOut | null;
};

export type ProviderKeyCreateRequest = {
  provider: string;
  key: string;
  nickname?: string | null;
};
