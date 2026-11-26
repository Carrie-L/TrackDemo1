/** 埋点小岛 Tailwind 配置 —— 改了 index.html / app.js 里的样式类后执行 web/build_css.cmd 重新生成 static/app.css */
module.exports = {
  content: ["./index.html", "./app.js"],
  theme: {
    extend: {
      colors: {
        cream: { DEFAULT: "#FFFEFB", 2: "#F3FAFE", 3: "#EBF5FC" },
        ink: { DEFAULT: "#58483C", soft: "#86776B", faint: "#B4A69A" },
        wood: { 50: "#FAF7F2", 100: "#F2EBE0", 200: "#E5D8C5", 300: "#D5C0A2", 400: "#C1A17C", 500: "#A68059", 600: "#856241", 700: "#664A30" },
        leaf: { 50: "#F0F9EC", 100: "#E2F4DA", 200: "#C7E7BA", 300: "#A3D490", 400: "#7DC067", 500: "#63A950", 600: "#4D8E3E", 700: "#3C7130" },
        pond: { 50: "#F2FAFF", 100: "#E1F4FE", 200: "#C7E9FB", 300: "#9FD8FA", 500: "#6BC6F5", 600: "#5EB8EE", 700: "#664A30" },
        sakura: { 50: "#FFF7FB", 100: "#FDECF4", 200: "#FADCEB", 300: "#F0BAD7", 400: "#E8A4C7", 500: "#E294BC", 600: "#A65A7D", 700: "#7F4A64" },
        peach: { 50: "#FFF5ED", 100: "#FDE7D7", 200: "#F8CFB2", 500: "#E5925C", 600: "#C9763F" },
        berry: { 50: "#FFF3F5", 100: "#FDE2E7", 200: "#F6C4CE", 300: "#F2AEBB", 400: "#EA91A2", 500: "#E2788C", 600: "#C95A70", 700: "#A13E52" },
        lilac: { 50: "#F7F3FC", 100: "#EEE6F9", 200: "#DCCCF1", 500: "#9A7FD0", 600: "#7F63B8" },
        sun: { 50: "#FFFAEA", 100: "#FDF1C9", 200: "#F8E094", 500: "#DDA92C", 600: "#B98914", 700: "#8F6A0E" },
      },
      fontSize: {
        xs: ["12.5px", "1.5"],
        sm: ["13.5px", "1.55"],
        base: ["14.5px", "1.6"],
        lg: ["16px", "1.45"],
        xl: ["18px", "1.4"],
        "2xl": ["21px", "1.3"],
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
