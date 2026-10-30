import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // New in react-hooks v7's recommended-latest; over-eager for standard
      // patterns like resetting state when a prop changes or toggling a
      // loading flag before a fetch. The genuinely useful rules
      // (rules-of-hooks, exhaustive-deps, refs) stay on.
      'react-hooks/set-state-in-effect': 'off',
    },
  },
  {
    // shadcn/ui primitives export variant helpers alongside the component, and
    // lib modules (e.g. the settings context) export a provider plus its hook.
    // Neither is a real fast-refresh problem.
    files: ['src/components/ui/**', 'src/lib/**'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
])
