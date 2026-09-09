import js from '@eslint/js';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';

export default [
  js.configs.recommended,
  {
    files: ['src/**/*.{js,jsx}'],
    plugins: {
      react,
      'react-hooks': reactHooks
    },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.browser,
        ...globals.es2021,
        ...globals.node,
        ...globals.vitest
      },
      parserOptions: {
        ecmaFeatures: {
          jsx: true
        }
      }
    },
    settings: {
      react: {
        version: 'detect'
      }
    },
    rules: {
      // Nested components - warn level (many existing icons, but critical ones monitored)
      // YouTubeEmbed is already at module level. Other nested components are less critical.
      // The pre-commit hook specifically guards YouTubeEmbed position.
      'react/no-unstable-nested-components': 'warn',

      // React Hooks rules
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',

      // React best practices
      'react/jsx-uses-vars': 'error',
      'react/prop-types': 'warn',
      'react/jsx-key': 'error',
      'react/jsx-no-duplicate-props': 'error',

      // General JS rules - relaxed for existing code
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
      'no-console': 'off'  // Allow console for debugging
    }
  },
  {
    // Ignore patterns
    ignores: ['dist/**', 'node_modules/**', '*.config.js']
  }
];
