import { expect, test, type Page } from "@playwright/test";

// Общий мок API-контракта (ответы соответствуют api/types.ts).
async function mockApi(page: Page, opts: { authenticated: boolean; users?: Record<string, string>; guildId?: string }) {
  const guildId = opts.guildId ?? "77";
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const me = {
      authenticated: opts.authenticated,
      authEnabled: true,
      loginUrl: "/api/auth/login",
      user: opts.authenticated ? { userId: "u1", userName: "tester" } : undefined,
      access: opts.authenticated
        ? [{ guildId, canRead: true, canWrite: true }]
        : [],
    };
    const send = (json: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });
    if (path === "/api/auth/whoami") return send(me);
    // ЧЕСТЬ сервера (T03: reads fail-closed): без сессии guild-endpoint'ы отдают
    // 401 — UI обязан показать ошибку/вход, а не данные.
    if (!opts.authenticated && path.startsWith("/api/guild/"))
      return send({ detail: "unauthorized" }, 401);
    if (path === "/api/guilds")
      return send({
        guilds: opts.authenticated
          ? [{ guildId, name: "Test Guild", canRead: true, canWrite: true }]
          : [],
      });
    if (path === "/api/healthz") return send({ ok: true });
    if (path === "/api/readyz") return send({ ok: true });
    if (path.endsWith("/names"))
      return send({ guildId, guildName: "Test Guild", channels: {}, roles: {}, users: opts.users ?? {}, userColors: {} });
    if (path.endsWith("/leaderboard"))
      return send({
        guildId,
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
  // прямой заход: сервер без сессии отдаёт 401 (мок честный), UI не показывает данные
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

test("имя сервера заменяет сохранённый старый ник в лидерборде", async ({ page }) => {
  await mockApi(page, { authenticated: true, guildId: "77777", users: { u1: "Новый ник" } });
  await page.goto("/g/77777/leaderboard");
  await expect(page.getByRole("row", { name: /Новый ник/ }).first()).toBeVisible();
  await expect(page.getByRole("row", { name: /bob/ }).first()).toBeVisible();
});

for (const width of [320, 390, 768]) {
  test(`лидерборд помещается в мобильный viewport ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await mockApi(page, { authenticated: true, guildId: "77777" });
    await page.goto("/g/77777/leaderboard");
    await expect(page.getByRole("row", { name: /alice/ }).first()).toBeVisible();
    const overflow = await page.evaluate(() => ({
      delta: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      offenders: [...document.querySelectorAll("body *")]
        .filter((element) => element.getBoundingClientRect().right > innerWidth + 1)
        .slice(0, 8)
        .map((element) => ({ tag: element.tagName, className: element.className, right: element.getBoundingClientRect().right })),
    }));
    expect(overflow.delta, JSON.stringify(overflow.offenders)).toBeLessThanOrEqual(1);
    await expect(page.getByRole("button", { name: "следующая страница" })).toBeVisible();
  });
}
