# Website frontend tests

## Requirement

Add deterministic website tests so the dashboard UI/API wiring is covered before calling the dashboard surface complete.

## Research

### Codemigo knowledge/usecase

- Build browser-facing tests around the specific user flows that matter, especially interactive dashboard actions and API mutations.
- Keep tests close to the behavior contract: verify visible UI and emitted requests rather than implementation internals.
- Prefer fast component/integration tests for client components, with E2E reserved for full browser flows that require the running app.

### Next.js docs

The local Next 16 documentation in `website/node_modules/next/dist/docs/01-app/02-guides/testing/vitest.md` recommends Vitest and React Testing Library for unit/component tests, with:

- `vitest`
- `@vitejs/plugin-react`
- `jsdom`
- `@testing-library/react`
- `@testing-library/dom`
- TypeScript path alias support. The local docs recommend `vite-tsconfig-paths`;
  the installed Vite version also supports native `resolve.tsconfigPaths: true`.

The same docs note that async Server Components are better covered by E2E tests. The dashboard page is a client component, so Vitest/RTL can exercise it without starting a Next server.

### Current project state

- `website/package.json` has `build`, `dev`, `start`, and `lint`, but no `test` script.
- The lint script is currently just `eslint`, which exits with help output instead of checking the codebase. It should target `.`.
- No first-party Vitest/Jest/Playwright config or tests were present before this work.

## Test strategy

- Add a Vitest config with React, jsdom, and tsconfig path support.
- Add a small setup file that runs RTL cleanup after each test.
- Mock `authFetch`, `useAuth`, and Next navigation in tests so dashboard behavior is deterministic.
- Cover initial dashboard fetches for all backend-backed sections.
- Cover representative mutations for each feature group:
  - task update/complete/snooze/unsnooze/delete,
  - goal create/update/complete/abandon/delete,
  - profile settings save,
  - call-window create/update/delete,
  - call-history filters.

## Gotchas

- Route groups in import paths, such as `(app)`, are valid in test imports but need quoting as normal string paths.
- Browser confirmation prompts should be mocked.
- Avoid relying on exact dynamic timestamps; assert request paths and request bodies for mutation coverage.

## Relevant links

- Official Next.js local docs: `website/node_modules/next/dist/docs/01-app/02-guides/testing/vitest.md`
- Official Next.js local docs: `website/node_modules/next/dist/docs/01-app/02-guides/testing/index.md`

## Required packages

Development-only:

- `vitest`
- `@vitejs/plugin-react`
- `jsdom`
- `@testing-library/react`
- `@testing-library/dom`
- Native Vite `resolve.tsconfigPaths: true` is used instead of
  `vite-tsconfig-paths` to avoid the Vite 8 deprecation warning.
