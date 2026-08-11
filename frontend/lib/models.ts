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
  // config/providers.yaml, slot `general` and `fast`
  "llama-3.3-70b-versatile": "Llama 3.3 70B",
  "llama-3.1-8b-instant": "Llama 3.1 8B",
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
