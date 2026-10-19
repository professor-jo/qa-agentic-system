### JavaScript / TypeScript (vitest)
- Name files *.test.js / *.test.ts (*.test.jsx / *.test.tsx for JSX). Globals (describe/it/expect/vi) are on.
- Import source with paths relative to the test file, e.g. "../../../src/utils/format.js".
- Mock modules with vi.mock(path, factory) and timers with vi.useFakeTimers().
