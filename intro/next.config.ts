import { fileURLToPath } from "node:url";
import type { NextConfig } from "next";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // This app lives inside a Python monorepo. Pin Turbopack to this app so
  // workspace discovery cannot accidentally select the repository root.
  turbopack: {
    root: projectRoot,
  },
};

export default nextConfig;
