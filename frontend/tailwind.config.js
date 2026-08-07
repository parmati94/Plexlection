/** @type {import('tailwindcss').Config} */
export default {
  content: ['./*.html', './partials/**/*.html', './js/**/*.js'],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Space Grotesk"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      colors: {
        // Mid-step between gray-700 and gray-800 for card surfaces.
        gray: { 750: '#2b3544' },
        // Accent resolves to CSS variables defined per [data-theme] in css/main.css,
        // so swapping the attribute on <html> re-tints every accent-* utility.
        // <alpha-value> keeps opacity modifiers (bg-accent-500/20) working.
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
        // Fixed semantic colors. Not themeable — meaning must stay constant.
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
    },
  },
  plugins: [],
};
