# Digital Detox

A Freedom-style website blocker for Windows. It blocks distracting sites at the
**hosts-file level**, so it works across **every browser** (Chrome / Edge /
Firefox …) with **no extension** to install. Add scheduled locking, an all-sites
block mode, a block-notice page, and a strict mode you can't wriggle out of.

> 中文說明在下方 · [Chinese docs below](#中文說明)

## Features

- **Instant lock** — 25 min / 1 hour / 3 hours / custom minutes, auto-unlocks when the timer ends
- **Block list** — enter a domain (e.g. `youtube.com`); the `www.` variant is blocked too
- **Scheduled locking** — pick weekdays + start/end time, supports overnight ranges (e.g. 22:00–06:00)
- **🌐 Block-all mode** — during a lock, block *everything* except a whitelist (default: `heptabase.com` and its subdomains), implemented via a system proxy auto-config (PAC), auto-reverted on unlock
- **Block notice page** — visiting a blocked site shows a page with a one-click "go write notes" button
- **Strict mode** — while locked you can't unlock early, remove sites, delete schedules, or turn off block-all
- **Start on login** — one toggle registers a Scheduled Task (highest privileges, at logon) so it starts silently in the background — no UAC prompt each boot
- Settings persist in `state.json`; schedules and lock state survive restarts

## Quick start

```
pip install -r requirements.txt
```

Run `start.bat` (it requests Administrator via UAC — writing to the hosts file
needs it). The control panel opens at http://localhost:8850

> Running `py app.py` without elevation still shows the UI, but it can't write
> to the hosts file — a yellow warning appears in the panel.

**Tip:** turn on **Start on login** from the panel and it runs automatically in
the background from then on, no UAC needed.

## Ports

| Port | Purpose |
|---|---|
| 8850 | Control panel + PAC file |
| 8851 | Blackhole proxy for block-all mode |
| 80 | Block notice page (hosts redirects here) |

## How it works

Blocked domains are written into `C:\Windows\System32\drivers\etc\hosts`
(pointing at `127.0.0.1`, inside a marked `# === DIGITAL-DETOX START/END ===`
block) and the DNS cache is flushed. Block-all mode sets a per-user proxy PAC
(`HKCU`, no admin needed) that sends everything except the whitelist to a local
blackhole proxy. Everything is reverted when the lock ends.

## Limitations (honest notes)

- **The notice page only appears over http.** `https://` connections are
  encrypted, so a custom page can't be injected — the browser shows its own
  connection error instead (still blocked, just no button). Big HSTS-preloaded
  sites (YouTube, Facebook…) are forced to https and only show the error page.
- Block-all mode relies on the system proxy setting, so it only affects programs
  that honor it (all mainstream browsers do). If a whitelisted app is missing
  resources, add that domain to the whitelist.
- After the program exits, existing hosts entries stay until next launch (this
  is intentional — it reinforces strict mode). A leftover proxy setting won't cut
  you off: browsers that can't fetch the PAC fall back to direct.
- Someone technical can edit hosts / proxy settings back by hand — this is a
  self-control aid, not tamper-proof security.
- Already-open tabs aren't killed, but reloads and new connections fail.

## License

MIT — see [LICENSE](LICENSE).

---

## 中文說明

把分心網站寫進 Windows hosts 檔導向本機，**所有瀏覽器（Chrome / Edge /
Firefox…）一起生效**，不需要裝任何瀏覽器外掛。

### 啟動

```
pip install -r requirements.txt
```

雙擊 `start.bat`（會跳出 UAC 要求系統管理員權限 → 按「是」），
瀏覽器會自動開啟 http://localhost:8850

> 直接 `py app.py` 也能開介面，但沒有管理員權限就寫不進 hosts，畫面上會出現黃色警告。
>
> **建議**：在控制台打開「開機自動啟動」，之後就會在背景自動執行、不用再碰 UAC。

### 功能

- **立即鎖定**：25 分 / 1 小時 / 3 小時 / 自訂分鐘數，倒數結束自動解鎖
- **封鎖清單**：輸入網域（如 `youtube.com`），會同時封鎖 `www.` 版本
- **定時排程**：選星期幾 + 開始/結束時間（支援跨夜，例如 22:00–06:00），時段內自動鎖定
- **🌐 全部封鎖模式**：鎖定時封鎖「所有」網站，只有白名單（預設 heptabase.com 及其子網域）可以連
  —— 透過系統代理自動設定（PAC）實作，解鎖時自動還原
- **封鎖提示頁**：瀏覽被封鎖的網站會看到提示頁，一鍵「📝 前往 Heptabase 寫筆記」
- **嚴格模式**：鎖定期間不能提前解除、不能移除網站、不能刪排程、不能關閉全部封鎖或加白名單
- **開機自動啟動**：一鍵在工作排程器建立「登入時以最高權限執行」的工作，開機自動背景啟動且不跳 UAC
- 設定存在 `state.json`，重開程式後排程與鎖定狀態仍有效

### 埠號

| 埠 | 用途 |
|---|---|
| 8850 | 控制介面 + PAC 檔 |
| 8851 | 全部封鎖模式的黑洞代理 |
| 80 | 封鎖提示頁（hosts 導向 127.0.0.1 後接住請求）|

### 限制（誠實說明）

- **提示頁只在 http 連線出現**。`https://` 連線因加密無法插入自訂頁面，會顯示瀏覽器的連線錯誤（一樣連不上，封鎖有效，只是看不到按鈕）。在網址列直接輸入網域時，多數網站會先試 https 失敗後退回 http 而看到提示頁；但大型網站（YouTube、Facebook 等有 HSTS 預載的）瀏覽器會強制 https，只會看到錯誤頁
- 全部封鎖模式靠系統代理設定，只影響「走系統代理」的程式（所有主流瀏覽器都是）；若 Heptabase 桌面版某些資源載不出來，把該網域加進白名單即可
- 程式關掉後 hosts 的封鎖仍保留（下次啟動才會依狀態解除）；系統代理若殘留，瀏覽器抓不到 PAC 會自動改走直連，不會斷網
- 懂技術的人可以手動改 hosts / 系統代理繞過——這是輔助自制力的工具，不是防駭工具
- 已開啟的分頁不會被踢掉，但重新整理或新連線就會失敗
