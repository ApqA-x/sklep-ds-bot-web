import { defineConfig } from "@playwright/test";

// T15/п.3: критичные U/G/A сценарии в браузере против СОБРАННОГО бандла.
// Бэкенд моканется на сетевом уровне (route), чтобы слой теста был честно
// про UI-контракт: сессия/gate и отрисовка лидерборда. Десятки JSX-зеркал
// здесь не пишутся — намеренно 2 сценария (gate + данные).
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "off",
  },
  webServer: {
    // --host 127.0.0.1 обязателен в CI: по умолчанию vite preview биндится на
    // «localhost», который на GitHub-раннере резолвится в ::1, а Playwright
    // опрашивает http://127.0.0.1 → webServer «никогда не поднимается»
    command: "npm run preview -- --port 4173 --strictPort --host 127.0.0.1",
    url: "http://127.0.0.1:4173/",
    reuseExistingServer: !process.env.CI,
  },
});
