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

for (const width of [320, 390]) {
  test(`мобильное меню доступно и закрывается на ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await mockApi(page, { authenticated: true });
    await page.goto("/g/77/leaderboard");
    const menu = page.getByRole("button", { name: "Разделы сервера" });
    const nav = page.getByRole("navigation", { name: "Разделы сервера" });
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await expect(nav).toBeHidden();
    await menu.click();
    await expect(menu).toHaveAttribute("aria-expanded", "true");
    await expect(nav.getByRole("link", { name: "Аудит" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Настройки" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await page.keyboard.press("Escape");
    await expect(nav).toBeHidden();
    await expect(menu).toBeFocused();
    await menu.click();
    await page.getByText("всего участников: 2").click();
    await expect(nav).toBeHidden();
    await expect(menu).toBeFocused();
    await menu.click();
    await nav.getByRole("link", { name: "Чат" }).click();
    await expect(page).toHaveURL(/\/g\/77\/chat$/);
    await expect(nav).toBeHidden();
  });
}

for (const width of [320, 390]) {
  test(`активные и архивные сессии читаются на ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await mockApi(page, { authenticated: true });
    await page.route(/\/api\/guild\/77\/sessions(?:\/|\?|$)/, (route) => {
      const path = new URL(route.request().url()).pathname;
      const userName = "Очень длинное имя участника без пробелов_12345678901234567890";
      const startedAt = "2026-10-08T19:00:00Z";
      const participant = { userId: "u1", userName, joinedAt: startedAt, durationMs: 3600000 };
      const summary = {
        id: "s1", channelId: "channel_12345678901234567890", startedAt,
        endedAt: "2026-10-08T20:00:00Z", endedByUserId: "u1", hasSummary: true,
      };
      const send = (json: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(json) });
      if (path.endsWith("/sessions/active")) return send({ guildId: "77", items: [{
        id: "s1", channelId: summary.channelId, startedAt, participants: [participant],
      }] });
      if (path.endsWith("/sessions/s1")) return send({
        ...summary, guildId: "77", status: "closed", updatedAt: summary.endedAt,
        summaryMessage: "Итоги встречи", summaryGeneratedAt: summary.endedAt,
        participants: [{ ...participant, leftAt: summary.endedAt, active: false }],
      });
      if (path.endsWith("/sessions")) return send({
        guildId: "77", status: "closed", page: 1, size: 25, total: 1, items: [summary],
      });
      return route.fallback();
    });
    const noPageOverflow = async () => {
      const overflow = await page.evaluate(() => ({
        delta: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        offenders: [...document.querySelectorAll("body *")]
          .filter((element) => element.getBoundingClientRect().right > innerWidth + 1)
          .slice(0, 8)
          .map((element) => ({ tag: element.tagName, className: element.className, text: element.textContent?.slice(0, 60) })),
      }));
      expect(overflow.delta, JSON.stringify(overflow.offenders)).toBeLessThanOrEqual(1);
    };
    await page.goto("/g/77/active");
    await expect(page.getByRole("button", { name: /подробности/ })).toBeVisible();
    await noPageOverflow();
    await page.getByRole("button", { name: /подробности/ }).click();
    await expect(page.locator(".card-details .responsive-board").first()).toBeVisible();
    await noPageOverflow();
    await page.goto("/g/77/sessions");
    await expect(page.locator(".responsive-board td .mobile-label", { hasText: "Саммари" })).toBeVisible();
    await noPageOverflow();
    await page.goto("/g/77/sessions/s1");
    await expect(page.locator(".responsive-board td .mobile-label", { hasText: "Время" })).toBeVisible();
    await noPageOverflow();
  });
}

for (const width of [320, 390]) {
  test(`чат с длинным текстом и медиа помещается на ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await mockApi(page, { authenticated: true });
    await page.route(/\/api\/guild\/77\/chat(?:\/|\?|$)/, (route) => {
      const path = new URL(route.request().url()).pathname;
      const send = (json: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(json) });
      if (path.endsWith("/chat/channels")) return send({ guildId: "77", items: [
        { channelId: "c1", count: 1, lastAt: "2026-10-08T19:00:00Z" },
      ] });
      if (path.endsWith("/chat")) return send({
        guildId: "77", channelId: "c1", hasMore: false, sort: "desc", nextCursor: null,
        items: [{
          messageId: "m1", channelId: "c1", authorUserId: "u2",
          authorName: "ОченьДлинноеИмяБезПробелов_123456789012345678901234567890",
          content: "НепрерывныйТекстСообщения_123456789012345678901234567890",
          sentAt: "2026-10-08T19:00:00Z", editedAt: "2026-10-08T19:05:00Z",
          deletedAt: "2026-10-08T19:10:00Z",
          attachments: [
            { id: "a1", filename: "очень-длинное-имя-файла_123456789012345678901234567890.txt", kind: "file", contentType: "text/plain", size: 42, path: "", stored: false, url: "data:text/plain,hello" },
            { id: "a2", filename: "image.png", kind: "image", contentType: "image/png", size: 42, path: "", stored: false, url: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='600' height='300'/%3E" },
            { id: "a3", filename: "clip.mp4", kind: "video", contentType: "video/mp4", size: 42, path: "", stored: false, url: "data:video/mp4;base64," },
          ],
        }],
      });
      return route.fallback();
    });
    await page.goto("/g/77/chat");
    await expect(page.locator(".chat-message")).toBeVisible();
    await expect(page.locator(".chat-image")).toBeVisible();
    await expect(page.locator(".chat-video")).toBeVisible();
    await expect(page.getByRole("button", { name: /Фильтры/ })).toBeVisible();
    const overflow = await page.evaluate(() => ({
      delta: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      feedDelta: (() => {
        const feed = document.querySelector(".chat-list");
        return feed ? feed.scrollWidth - feed.clientWidth : -1;
      })(),
      offenders: [...document.querySelectorAll("body *")]
        .filter((element) => element.getBoundingClientRect().right > innerWidth + 1)
        .slice(0, 8)
        .map((element) => ({ tag: element.tagName, className: element.className })),
    }));
    expect(overflow.delta, JSON.stringify(overflow.offenders)).toBeLessThanOrEqual(1);
    expect(overflow.feedDelta, JSON.stringify(overflow.offenders)).toBeLessThanOrEqual(1);
  });
}

for (const width of [320, 390]) {
  test(`таймер в профиле помещается в мобильный viewport ${width}px`, async ({ page }) => {
    const guildId = "77777";
    const userId = "111111111111111111";
    await page.setViewportSize({ width, height: 800 });
    await mockApi(page, { authenticated: true, guildId });
    await page.route(`**/api/guild/${guildId}/**`, (route) => {
      const path = new URL(route.request().url()).pathname;
      const send = (json: unknown) => route.fulfill({
        status: 200, contentType: "application/json", body: JSON.stringify(json),
      });
      if (path === `/api/guild/${guildId}/users/${userId}`) return send({
        guildId, userId, userName: "Спящий", period: "all", totalMs: 0,
        appearances: 0, messageCount: 0, invitedCount: 0,
        daily: [], dailyMessages: [], dailyInvites: [], roleIds: [], nicknames: [], join: null,
      });
      if (path === `/api/guild/${guildId}/users/${userId}/card`) return send({
        guildId, userId, source: "discord", username: "sleeper", globalName: "Спящий",
        nick: "Спящий", joinedAt: null, avatarUrl: "", bannerUrl: null,
        accentColor: null, avatars: [],
      });
      if (path === `/api/guild/${guildId}/users/${userId}/member`) return send({
        guildId, userId, source: "unavailable", roleIds: [], timeoutUntil: null, voiceChannelId: null,
      });
      if (path === `/api/guild/${guildId}/picker`) return send({
        guildId, roles: [], voiceChannels: [], textChannels: [],
      });
      if (path === `/api/guild/${guildId}/sleep/member/${userId}`) return send({
        guildId, userId, status: "pending", dueAt: "2026-10-08T23:00:00Z",
        hours: 2, resultAt: null, reason: null,
      });
      return route.fallback();
    });
    await page.goto(`/g/${guildId}/users/${userId}`);
    await expect(page.getByRole("heading", { name: "Автоотключение от голосового канала" })).toBeVisible();
    await expect(page.getByText("ожидает срока")).toBeVisible();
    const overflow = await page.evaluate(() => ({
      delta: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      offenders: [...document.querySelectorAll("body *")]
        .filter((element) => element.getBoundingClientRect().right > innerWidth + 1)
        .slice(0, 8)
        .map((element) => ({ tag: element.tagName, className: element.className })),
    }));
    expect(overflow.delta, JSON.stringify(overflow.offenders)).toBeLessThanOrEqual(1);
    await expect(page.getByRole("button", { name: "Поставить или заменить" })).toBeVisible();
    if (width === 320) {
      await page.getByRole("button", { name: "сбросить фильтр по пользователю" }).click();
      const search = page.getByPlaceholder("Ник или Discord ID");
      await search.fill("12345");
      await expect(search).toBeVisible();
      await search.fill("222222222222222222");
      await search.press("Enter");
      await expect(page.locator('.sleep-timer-picker .chip[title="222222222222222222"]')).toBeVisible();
    }
  });
}
