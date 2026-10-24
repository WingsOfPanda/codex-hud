// The accepted AP text-loader/setup, with all paths supplied by the pinned replay profile.
const source = process.env.AP_REPLAY_SOURCE!;
const modules = process.env.AP_REPLAY_NODE_MODULES!;
const recipe = process.env.AP_REPLAY_RECIPE!;
export default {
  root: source,
  cacheDir: `${process.env.AP_REPLAY_SCOPE}/vite-cache`,
  plugins: [{ name: "ap-text-loader", enforce: "pre",
    transform(code: string, id: string) {
      if (!/\.md(\?.*)?$/.test(id)) return null;
      return { code: `export default ${JSON.stringify(code)};`, map: null };
    } }],
  resolve: { alias: {
    "ap-replay/nudge": `${source}/src/commands/nudge.ts`,
    "ap-replay/tmux": `${source}/src/core/tmux.ts`,
    vitest: `${modules}/vitest/dist/index.js`,
  } },
  server: { fs: { allow: [source, modules, recipe] } },
  test: { include: [`${source}/tests/codex-plus-footer.test.ts`, `${recipe}/ap-release-replay-observer.test.ts`],
    environment: "node", setupFiles: [`${source}/tests/helpers/setupEnv.ts`],
    cache: false, pool: "forks", maxWorkers: 1, minWorkers: 1, fileParallelism: false },
};
