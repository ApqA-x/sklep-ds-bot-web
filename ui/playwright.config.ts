import { defineConfig } from "@playwright/test";

const port = process.env.E2E_PORT ?? "4173";

// T15/п.3: критичные U/G/A сценарии в браузере против СОБРАННОГО бандла.
// Бэкенд моканется на сетевом уровне (route), чтобы слой теста был честно
// про UI-контракт: сессия/gate и отрисовка лидерборда. Десятки JSX-зеркал
// здесь не пишутся — намеренно 2 сценария (gate + данные).
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "off",
  },
  webServer: {
    // --host 127.0.0.1 обязателен в CI: по умолчанию vite preview биндится на
    // «localhost», который на GitHub-раннере резолвится в ::1, а Playwright
    // опрашивает http://127.0.0.1 → webServer «никогда не поднимается»
    command: `npm run preview -- --port ${port} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: !process.env.CI,
  },
});
