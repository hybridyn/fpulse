/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        pulse: {
          50: '#FFFDF2',
          100: '#FFF8E1',
          200: '#FFECB3',
          300: '#FFD54F',
          400: '#FFCA28',
          500: '#F5A623',
          600: '#E8971C',
          700: '#D4880A',
          800: '#B87008',
          900: '#8B5A06',
        },
        pipe: {
          50: '#EBF4FF',
          100: '#DBEAFE',
          200: '#BFDBFE',
          300: '#93C5FD',
          400: '#60A5FA',
          500: '#3B7DD8',
          600: '#2E6BC4',
          700: '#1E5AAF',
          800: '#1E40AF',
          900: '#1E3A8A',
        },
        canvas: {
          bg: '#FFFBEB',   // lightened — was #FFF5D0 (BARLEY); amber-50 reads
                           // softer next to the charcoal thead and amber-100 chrome
          grid: '#FDE68A', // amber-200 — warm grid dots visible on the paler canvas
        },
        // Hybridyn brand tokens — theme v2 (see docs/DESIGN_THEME_V2.md)
        // Raw swatches. Semantic aliases below are preferred in component code.
        barley:  '#FFFBEB',   // canvas surface — lightened per Apr 20 feedback
                              // (was #FFF5D0; too saturated next to charcoal thead)
        naples:  '#FAD564',   // accent yellow (PROD thead text)
        navy:    '#1E3A8A',   // PROD thead bg (Tailwind blue-900)
        chrome:  '#FEF3C7',   // NEW — navbar + page header surface (amber-100).
                              // Warm enough to stand out against the paler canvas,
                              // ties to the amber thead text for palette coherence.
        // Semantic aliases.
        surface: {
          canvas:    '#FFFBEB',
          chrome:    '#FEF3C7',   // navbar + page header
          dashboard: '#FFFFFF',
          page:      '#FAFAFA',
        },
        // Theme v2 thead tokens — flat keys (nested keys with hyphens like
        // `thead: { 'dev-bg': ... }` confuse Tailwind's color-path resolver
        // and silently produce no classes).
        //
        // DEV  — slate-700 grey-blue charcoal + amber text. Confirmed in design review
        //        on Apr 20 as the correct DEV weight (matches ProjectsPage).
        //        Differentiation from PROD is by hue (grey-blue vs navy), not
        //        luminance. Both are dark enough to read as banners.
        'thead-dev-bg':     '#334155',   // slate-700
        'thead-dev-text':   '#FCD34D',   // amber-300
        'thead-dev-border': '#334155',   // matches header — outer table rule
        // PROD — deep navy blue-950 + NAPLES yellow. Matches the pre-theme-v2
        // gradient weight so PROD still feels heavy/serious.
        'thead-prod-bg':     '#172554',  // blue-950
        'thead-prod-text':   '#FAD564',  // NAPLES
        'thead-prod-border': '#172554',
      },
    },
  },
  plugins: [],
};
