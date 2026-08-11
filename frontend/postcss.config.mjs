/** Tailwind v4 ships as a PostCSS plugin; there is no tailwind.config.js —
 *  the design tokens live in `app/globals.css` under `@theme`. */
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
