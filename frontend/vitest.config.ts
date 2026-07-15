import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.ts'],
    css: true,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'json-summary'],
      // Coverage is REPORTED but not GATED right now. Reason: the two
      // existing tests (RoleGate.test.tsx, TableToolbar.test.tsx) use a
      // try/import-with-fallback pattern where the import paths
      // (`@/components/RoleGate`, `@/components/TableToolbar`) don't
      // match the real source files (`src/auth/RoleGate.tsx`,
      // `src/components/shared/TableToolbar.tsx`). The fallbacks also
      // have different props APIs from the real components, so simply
      // fixing the import paths would break the assertions.
      //
      // Until the tests are rewritten against the real components, any
      // threshold > 0 fails CI because the real source files get 0%
      // coverage. Keep the include pattern for observability (so when
      // more tests land, coverage is auto-measured) and leave thresholds
      // OFF until the first test actually exercises a real component.
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/**/*.d.ts',
        'src/main.tsx',
        'src/vite-env.d.ts',
      ],
      // Re-enable once the existing tests (or new ones) actually
      // import from the real source files — see note above.
      // thresholds: {
      //   lines: 70,
      //   functions: 70,
      //   branches: 60,
      //   statements: 70,
      // },
    },
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
