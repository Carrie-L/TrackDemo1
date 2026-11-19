@echo off
rem Rebuild static/app.css after changing Tailwind classes in index.html / app.js (needs Node.js)
cd /d %~dp0
npx -y tailwindcss@3.4.17 -c tailwind.config.js -i src/input.css -o static/app.css --minify
