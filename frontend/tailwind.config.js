/** @type {import('tailwindcss').Config} */
export default {
  content: ['./*.html', './partials/**/*.html', './js/**/*.js'],
  theme: {
    extend: {
      // IBM Plex, self-hosted. Mono carries display AND data: the typography of
      // slates, edit decision lists and ffprobe output, which is what this tool
      // is actually about. Sans handles prose so the mono stays a deliberate
      // accent rather than a gimmick.
      fontFamily: {
        display: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
        sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },

      // A real scale with a top end. The old UI had 211 of 229 sized elements at
      // 14px or smaller and nothing above 24px, which is why it read as flat.
      // 13px is the floor for anything meant to be read; 11px is reserved for
      // uppercase micro-labels where letterspacing does the work.
      fontSize: {
        micro: ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.05em', fontWeight: '500' }],
        'body-s': ['0.8125rem', { lineHeight: '1.25rem' }],
        body: ['0.9375rem', { lineHeight: '1.5rem' }],
        title: ['1.125rem', { lineHeight: '1.6rem', letterSpacing: '-0.01em' }],
        'display-m': ['1.5rem', { lineHeight: '1.85rem', letterSpacing: '-0.015em' }],
        'display-l': ['2rem', { lineHeight: '2.35rem', letterSpacing: '-0.022em' }],
      },

      colors: {
        // Graphite with a slight cool cast — the neutral of a grading suite
        // rather than the flat near-black every dark theme defaults to. Four
        // steps so grouping reads through elevation, not just borders.
        sunken: '#0A0D0F',
        surface: '#101417',
        raised: '#171C20',
        overlay: '#1D2429',
        line: {
          DEFAULT: '#232A30',
          strong: '#333C44',
        },
        ink: {
          DEFAULT: '#E6EBEF',
          muted: '#94A3AD',
          faint: '#61707A',
        },

        // Accent resolves to CSS variables swapped by [data-theme] on <html>.
        accent: {
          50: 'rgb(var(--accent-50) / <alpha-value>)',
          100: 'rgb(var(--accent-100) / <alpha-value>)',
          200: 'rgb(var(--accent-200) / <alpha-value>)',
          300: 'rgb(var(--accent-300) / <alpha-value>)',
          400: 'rgb(var(--accent-400) / <alpha-value>)',
          500: 'rgb(var(--accent-500) / <alpha-value>)',
          600: 'rgb(var(--accent-600) / <alpha-value>)',
          700: 'rgb(var(--accent-700) / <alpha-value>)',
          800: 'rgb(var(--accent-800) / <alpha-value>)',
          900: 'rgb(var(--accent-900) / <alpha-value>)',
        },

        // Fixed semantic colours. Never themeable — danger must not become the
        // accent hue.
        ok: {
          400: 'rgb(var(--ok-400) / <alpha-value>)',
          500: 'rgb(var(--ok-500) / <alpha-value>)',
          600: 'rgb(var(--ok-600) / <alpha-value>)',
          900: 'rgb(var(--ok-900) / <alpha-value>)',
        },
        warn: {
          400: 'rgb(var(--warn-400) / <alpha-value>)',
          500: 'rgb(var(--warn-500) / <alpha-value>)',
          600: 'rgb(var(--warn-600) / <alpha-value>)',
          900: 'rgb(var(--warn-900) / <alpha-value>)',
        },
        danger: {
          400: 'rgb(var(--danger-400) / <alpha-value>)',
          500: 'rgb(var(--danger-500) / <alpha-value>)',
          600: 'rgb(var(--danger-600) / <alpha-value>)',
          900: 'rgb(var(--danger-900) / <alpha-value>)',
        },
      },

      spacing: {
        rail: '13rem',
      },
    },
  },
  plugins: [],
};
