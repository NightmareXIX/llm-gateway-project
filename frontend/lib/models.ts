/**
 * Display names for providers and models.
 *
 * One rule governs this file: **an unknown model falls through to its wire
 * name.** A gateway whose whole premise is "we will tell you honestly which
 * model answered" cannot render a model it does not recognise as "Unknown" or,
 * worse, silently prettify it into something else. The map is a courtesy for the
 * handful of models actually configured; everything else shows exactly what came
 * off the wire, which is also what makes a new entry in `providers.yaml` visible
 * in the UI on the first request rather than after a frontend deploy.
 */

const PROVIDER_LABELS: Record<string, string> = {
  groq: "Groq",
  gemini: "Google",
  openrouter: "OpenRouter",
};

const MODEL_LABELS: Record<string, string> = {
  // config/providers.yaml, slots `general` and `fast` — three candidates each
  // since Phase 2 Step 6, which is when substitution became something a user can
  // actually see under a message.
  "llama-3.3-70b-versatile": "Llama 3.3 70B",
  "llama-3.1-8b-instant": "Llama 3.1 8B",
  "gemini-3.6-flash": "Gemini 3.6 Flash",
  "gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite",
  // The `:free` suffix is load-bearing on the wire (dropping it routes to the
  // paid variant) and noise on screen, so it is dropped here and only here.
  "nvidia/nemotron-3-super-120b-a12b:free": "Nemotron 3 Super 120B",
  "openai/gpt-oss-20b:free": "GPT-OSS 20B",
};

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

export function modelLabel(model: string): string {
  return MODEL_LABELS[model] ?? model;
}

/** Slot names come from YAML and are shown verbatim, in mono, as identifiers. */
export function slotLabel(slot: string): string {
  return slot;
}
