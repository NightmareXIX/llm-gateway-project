import type { NextConfig } from "next";

/**
 * The browser never talks to the gateway directly.
 *
 * Every API call goes to `/api/gw/*` on this origin and is rewritten to the
 * FastAPI service. That keeps requests same-origin, which means no preflight,
 * no `Access-Control-Allow-*` surface to get wrong, and — the reason that
 * matters here — no change to `app/main.py`, which mounts no CORS middleware.
 *
 * 127.0.0.1 rather than localhost by default: on Windows the latter resolves to
 * ::1 first and the first connection to a published Docker port stalls ~20s
 * before falling back. Same note as the backend's own .env.example.
 */
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [{ source: "/api/gw/:path*", destination: `${GATEWAY_URL}/:path*` }];
  },
};

export default nextConfig;
