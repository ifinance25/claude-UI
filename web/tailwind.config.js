/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "ui-sans-serif",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "sans-serif",
        ],
        display: [
          "Gilroy",
          "ui-sans-serif",
          "-apple-system",
          "sans-serif",
        ],
      },
      fontSize: {
        // Bump baseline ~1px so every text class reads bigger.
        xs: ["0.8125rem", { lineHeight: "1.15rem" }],     // 13px
        sm: ["0.9375rem", { lineHeight: "1.4rem" }],      // 15px
        base: ["1rem", { lineHeight: "1.6rem" }],         // 16px
        lg: ["1.125rem", { lineHeight: "1.75rem" }],      // 18px
        xl: ["1.3125rem", { lineHeight: "1.85rem" }],     // 21px
        "2xl": ["1.625rem", { lineHeight: "2.1rem" }],    // 26px
        "3xl": ["2rem", { lineHeight: "2.4rem" }],        // 32px
        "4xl": ["2.5rem", { lineHeight: "2.9rem" }],      // 40px
      },
      spacing: {
        "18": "4.5rem",
        "88": "22rem",
      },
      borderRadius: {
        "2xl": "1.125rem",
        "3xl": "1.5rem",
      },
      keyframes: {
        fadeIn: { "0%": { opacity: "0" }, "100%": { opacity: "1" } },
        // «feel better»: появление через лёгкий blur + подъём — текст
        // «проявляется», а не дёргается. Анимация одноразовая (на маунт бабла).
        fadeInUp: {
          "0%": { opacity: "0", transform: "translateY(6px)", filter: "blur(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)", filter: "blur(0)" },
        },
        // Более выраженный вход для крупных экранов при первом показе
        // (пустой экран чата, страница входа).
        riseIn: {
          "0%": { opacity: "0", transform: "translateY(8px)", filter: "blur(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)", filter: "blur(0)" },
        },
        // «feel better» (icon animations): иконка «выскакивает» — scale+blur+opacity
        // с лёгким overshoot (spring-ощущение). Для появления иконок/логотипа.
        iconPop: {
          "0%": { opacity: "0", transform: "scale(0.6)", filter: "blur(4px)" },
          "100%": { opacity: "1", transform: "scale(1)", filter: "blur(0)" },
        },
        slideUpIn: {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        scaleIn: {
          "0%": { opacity: "0", transform: "scale(0.96)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        blink: { "0%,100%": { opacity: "1" }, "50%": { opacity: "0" } },
      },
      animation: {
        fadeIn: "fadeIn 200ms ease-out",
        fadeInUp: "fadeInUp 320ms cubic-bezier(0.25, 0.46, 0.45, 0.94)",
        riseIn: "riseIn 640ms cubic-bezier(0.25, 0.46, 0.45, 0.94)",
        iconPop: "iconPop 380ms cubic-bezier(0.34, 1.56, 0.64, 1)",
        slideUpIn: "slideUpIn 260ms ease-out",
        scaleIn: "scaleIn 200ms ease-out",
        blink: "blink 1s steps(2,start) infinite",
      },
      // Кривая со spring-overshoot — для отклика иконок (ease-spring на hover/повороте).
      transitionTimingFunction: {
        spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
    },
  },
  plugins: [],
};
