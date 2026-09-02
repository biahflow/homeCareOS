import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `@homecareos/contracts` é um workspace TS puro, sem build próprio: o
  // Next transpila o código-fonte dele junto com o app.
  transpilePackages: ["@homecareos/contracts"],
};

export default nextConfig;
