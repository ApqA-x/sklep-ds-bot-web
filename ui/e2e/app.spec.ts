import { expect, test, type Page } from "@playwright/test";

// Общий мок API-контракта (ответы соответствуют api/types.ts).
async function mockApi(page: Page, opts: { authenticated: boolean }) {
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const me = {
      authenticated: opts.authenticated,
      authEnabled: true,
      loginUrl: "/api/auth/login",
      user: opts.authenticated ? { userId: "u1", userName: "tester" } : undefined,
      access: opts.authenticated
        ? [{ guildId: "77", canRead: true, canWrite: true }]
        : [],
    };
    const send = (json: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });
    if (path === "/api/auth/whoami") return send(me);
    if (path === "/api/guilds")
      return send({
        guilds: opts.authenticated
          ? [{ guildId: "77", name: "Test Guild", canRead: true, canWrite: true }]
          : [],
      });
    if (path === "/api/healthz") return send({ ok: true });
    if (path === "/api/readyz") return send({ ok: true });
    if (path.endsWith("/leaderboard"))
      return send({
        guildId: "77",
        period: "7d",
        limit: 50,
        page: 1,
        total: 2,
        cached: false,
        items: [
          { userId: "u1", userName: "alice", totalMs: 7200000, appearances: 9 },
          { userId: "u2", userName: "bob", totalMs: 3600000, appearances: 4 },
        ],
      });
    return send({ detail: "not mocked in e2e" }, 501);
  });
}

test("не-аутентифицированный пользователь видит вход, а не данные (#T02 gate)", async ({ page }) => {
  await mockApi(page, { authenticated: false });
  await page.goto("/");
  await expect(page.getByText("Войти через Discord")).toBeVisible();
  // прямой заход на защищённый экран не показывает данные
  await page.goto("/g/77/leaderboard");
  await expect(page.getByText("alice")).toHaveCount(0);
});

test("лидерборд рендерит смоканные строки для авторизованной гильдии", async ({ page }) => {
  await mockApi(page, { authenticated: true });
  await page.goto("/g/77/leaderboard");
  await expect(page.getByText("всего участников: 2")).toBeVisible();
  await expect(page.getByRole("row", { name: /alice/ }).first()).toBeVisible();
  await expect(page.getByRole("row", { name: /bob/ }).first()).toBeVisible();
});
