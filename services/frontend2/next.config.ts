import type { NextConfig } from "next";

// Single knob for the web path this app is served under, read from the
// NEXT_PUBLIC_BASE_PATH build-time env (default "" = web root). src/lib/route-paths.ts
// reads the SAME var, so the whole app stays in sync from one place. We read the
// env here rather than importing route-paths because next.config is required at
// runtime by `next start`, and src/ is not present in the production runner image.
// Next.js requires basePath to be omitted (not "") when serving at the root.
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const nextConfig: NextConfig = {
  ...(BASE_PATH ? { basePath: BASE_PATH } : {}),
  reactStrictMode: true,
  typedRoutes: false,
};

export default nextConfig;
