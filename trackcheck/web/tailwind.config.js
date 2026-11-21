/** 埋点小岛 Tailwind 配置 —— 改了 index.html / app.js 里的样式类后执行 web/build_css.cmd 重新生成 static/app.css */
module.exports = {
  content: ["./index.html", "./app.js"],
  theme: {
    extend: {
      colors: {
        cream: { DEFAULT: "#FFFBF0", 2: "#F8F1E0", 3: "#F1E7D0" },
        ink: { DEFAULT: "#5B4636", soft: "#8C7763", faint: "#B9A894" },
        wood: { 50: "#FBF5EA", 100: "#F4E7D1", 200: "#E8D4B3", 300: "#D7B78C", 400: "#C39566", 500: "#A97B4F", 600: "#8A633F", 700: "#6B4C31" },
        leaf: { 50: "#F2F9EB", 100: "#E2F1D4", 200: "#C8E4B0", 300: "#A6D186", 400: "#84BB64", 500: "#68A34C", 600: "#558A3D", 700: "#436E31" },
        pond: { 50: "#F1FAFF", 100: "#DFF4FE", 200: "#C7E9FB", 300: "#9FD8FA", 500: "#5CC0F2", 600: "#389FD8", 700: "#2884BA" },
        sakura: { 50: "#FFF5FA", 100: "#FDE5F2", 200: "#F7CEE4", 300: "#F0BAD7", 400: "#E49BC2", 500: "#DA7CAE", 700: "#7E2E58" },
        peach: { 50: "#FFF5ED", 100: "#FDE7D7", 200: "#F8CFB2", 500: "#E5925C", 600: "#C9763F" },
        berry: { 50: "#FFF3F5", 100: "#FDE2E7", 200: "#F6C4CE", 500: "#E2788C", 600: "#C95A70", 700: "#A13E52" },
        lilac: { 50: "#F7F3FC", 100: "#EEE6F9", 200: "#DCCCF1", 500: "#9A7FD0", 600: "#7F63B8" },
        sun: { 50: "#FFFAEA", 100: "#FDF1C9", 200: "#F8E094", 500: "#DDA92C", 600: "#B98914", 700: "#8F6A0E" },
      },
      fontSize: {
        xs: ["11.5px", "1.45"],
        sm: ["12.5px", "1.5"],
        base: ["13.5px", "1.55"],
        lg: ["15px", "1.4"],
        xl: ["17px", "1.35"],
        "2xl": ["20px", "1.25"],
      },
      fontFamily: {
        sans: ['"Nunito"', '"Quicksand"', '"PingFang SC"', '"HarmonyOS Sans SC"', '"Microsoft YaHei UI"', '"Microsoft YaHei"', "sans-serif"],
        mono: ['"JetBrains Mono"', '"Cascadia Code"', "Consolas", "monospace"],
      },
      boxShadow: {
        soft: "0 6px 20px -8px rgba(138, 99, 63, .25)",
        wood: "0 4px 0 0 #E8D4B3",
        leafbtn: "0 3px 0 0 #436E31",
        woodbtn: "0 3px 0 0 #D7B78C",
      },
    },
  },
  plugins: [],
};
