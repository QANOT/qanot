import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: false,
    environment: "node",
    include: ["test/**/*.test.ts"],
    testTimeout: 10000,
    pool: "forks",
    poolOptions: {
      forks: {
        singleFork: false,
      },
    },
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.ts"],
      exclude: ["src/types.ts"],
    },
  },
});
