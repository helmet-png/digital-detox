# -*- coding: utf-8 -*-
"""
Digital Detox — Windows 網站鎖定工具（類 Freedom）
原理：把封鎖清單寫進系統 hosts 檔（導向 0.0.0.0），所有瀏覽器一起生效。
需要以「系統管理員」身分執行（用 start.bat 啟動會自動要求提權）。

py app.py → http://localhost:8850
"""

import ctypes
import json
import math
import sys
import os
import re
import subprocess
import threading
import time
import webbrowser
import winreg
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flask import Flask, jsonify, request

PORT = 8850          # 控制介面
PROXY_PORT = 8851    # 全部封鎖模式的黑洞代理
BLOCK_PAGE_PORT = 80 # 封鎖提示頁（hosts 導向 127.0.0.1 後由這裡接住）
HEPTABASE_URL = "https://app.heptabase.com"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
MARK_START = "# === DIGITAL-DETOX START (do not edit) ==="
MARK_END = "# === DIGITAL-DETOX END ==="

app = Flask(__name__)
state_lock = threading.Lock()


# ---------- 狀態存取 ----------

DEFAULT_STATE = {
    "sites": ["facebook.com", "youtube.com", "instagram.com"],
    "schedules": [],   # {"id": 1, "days": [0..6] (0=一), "start": "09:00", "end": "12:00"}
    "strict": False,
    "lock_until": 0,   # 手動鎖定的截止 timestamp
    "skip_until": 0,   # 非嚴格模式下「跳過目前排程」的截止 timestamp
    "next_id": 1,
    "block_all": False,             # 鎖定時封鎖「所有」網站（僅白名單可連）
    "allow_sites": ["heptabase.com"],  # 全部封鎖模式的白名單（含其子網域）
    "pomo": None,  # 番茄鐘 {"start": ts, "focus": 分, "break": 分, "cycles": 顆數}
    # 緊急使用：每次封鎖期間可暫時解除 EMERGENCY_MAX 次，每次 EMERGENCY_MINUTES 分鐘。
    # armed=目前正處於一段封鎖期間（用來判斷何時該把次數歸零，可跨重啟）
    "emergency": {"used": 0, "until": 0, "armed": False},
    # 使用額度（被動）：每個 window_hours 小時的視窗內，只有「原本不會被鎖」的時段
    # 才會消耗 minutes 分鐘的額度；額度歸零則在該視窗剩餘時間內強制鎖定。
    # window_start=目前視窗起點；used_seconds/synced_at/was_free=記帳用檢查點，
    # 只在轉換時刻（視窗重置、空閒⇄可用切換）才寫入 state.json，平常不寫檔省電。
    "budget": {
        "enabled": False, "window_hours": 3.0, "minutes": 30.0,
        "window_start": 0.0, "used_seconds": 0.0, "synced_at": 0.0, "was_free": False,
    },
}

EMERGENCY_MAX = 2        # 每次封鎖期間可用次數
EMERGENCY_MINUTES = 5    # 每次時長
BUDGET_MIN_WINDOW_HOURS = 0.5
BUDGET_MAX_WINDOW_HOURS = 24 * 30

# 版本識別：程式檔改動時間。開著的分頁偵測到與伺服器不符會自動重載，
# 避免更新後還在用舊 HTML（按鈕點了沒反應）。
APP_BUILD = str(int(os.path.getmtime(os.path.abspath(__file__))))


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        em = dict(DEFAULT_STATE["emergency"])   # 舊版 state.json 可能沒有或缺欄位
        if isinstance(merged.get("emergency"), dict):
            em.update(merged["emergency"])
        merged["emergency"] = em
        bg = dict(DEFAULT_STATE["budget"])
        if isinstance(merged.get("budget"), dict):
            bg.update(merged["budget"])
        merged["budget"] = bg
        return merged
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


state = load_state()


# ---------- 鎖定判斷 ----------

def parse_hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def active_schedule_end(st, now=None):
    """若目前落在某個排程時段內，回傳該時段今天的結束 timestamp；否則回傳 None。支援跨夜。"""
    now = now or datetime.now()
    minutes_now = now.hour * 60 + now.minute
    weekday = now.weekday()  # 0=週一
    best_end = None
    for sch in st["schedules"]:
        start, end = parse_hm(sch["start"]), parse_hm(sch["end"])
        if start == end:
            continue
        if start < end:  # 一般時段
            hit = weekday in sch["days"] and start <= minutes_now < end
            end_day = now
        else:  # 跨夜時段，例如 22:00–06:00
            if weekday in sch["days"] and minutes_now >= start:
                hit, end_day = True, None  # 結束在明天
            elif ((weekday - 1) % 7) in sch["days"] and minutes_now < end:
                hit, end_day = True, now
            else:
                hit = False
        if not hit:
            continue
        end_dt = now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
        if end_day is None:
            end_dt = end_dt + timedelta(days=1)
        ts = end_dt.timestamp()
        if best_end is None or ts > best_end:
            best_end = ts
    return best_end


def pomo_status(st, now_ts=None):
    """番茄鐘目前階段。回傳 (phase, cycle, phase_end)；未啟動或已跑完回 (None, 0, None)。
    純粹由開始時間+設定推算，重啟程式不影響。最後一顆專注結束即整個流程結束（不含休息）。"""
    p = st.get("pomo")
    if not p:
        return None, 0, None
    now_ts = now_ts or time.time()
    focus_s, break_s = p["focus"] * 60, p["break"] * 60
    cycle_s = focus_s + break_s
    total_s = p["cycles"] * cycle_s - break_s
    elapsed = now_ts - p["start"]
    if elapsed < 0 or elapsed >= total_s:
        return None, 0, None
    idx = int(elapsed // cycle_s)          # 第幾顆（0 起算）
    pos = elapsed % cycle_s
    if pos < focus_s:
        return "focus", idx + 1, p["start"] + idx * cycle_s + focus_s
    return "break", idx + 1, p["start"] + (idx + 1) * cycle_s


def next_schedule_start_ts(st, now_dt):
    """下一個排程時段的開始 timestamp；沒有排程回 None。"""
    best = None
    for sch in st["schedules"]:
        start_min = parse_hm(sch["start"])
        for delta in range(8):  # 最多掃一週
            day = now_dt + timedelta(days=delta)
            if day.weekday() not in sch["days"]:
                continue
            cand = day.replace(hour=start_min // 60, minute=start_min % 60,
                               second=0, microsecond=0).timestamp()
            if cand > now_dt.timestamp():
                best = cand if best is None else min(best, cand)
                break
    return best


def seconds_to_next_change(st, now_ts=None):
    """距離下一次鎖定狀態可能改變的秒數（省電用：沒有邊界前不需要醒來）。
    邊界＝手動鎖到期、番茄鐘階段切換、排程時段結束、下一個排程開始。無邊界回 None。"""
    now_ts = now_ts or time.time()
    now_dt = datetime.fromtimestamp(now_ts)
    cands = []
    if emergency_active(st, now_ts):
        cands.append(st["emergency"]["until"])  # 緊急使用到期要立刻鎖回去
    if st["lock_until"] > now_ts:
        cands.append(st["lock_until"])
    phase, _, phase_end = pomo_status(st, now_ts)
    if phase:
        cands.append(phase_end)
    end = active_schedule_end(st, now_dt)
    if end:
        cands.append(end)
    nxt = next_schedule_start_ts(st, now_dt)
    if nxt:
        cands.append(nxt)
    if st["budget"]["enabled"]:
        base_locked, _, _ = base_lock_status(st, now_ts)
        if not base_locked:  # 額度只在「本來自由」的時段才有意義
            _, _, remaining, window_end = _budget_effective(st, now_ts)
            cands.append(now_ts + remaining if remaining > 0 else window_end)
    if not cands:
        return None
    return max(0.0, min(cands) - now_ts)


def emergency_left(st):
    return max(0, EMERGENCY_MAX - st["emergency"]["used"])


def emergency_active(st, now_ts=None):
    return st["emergency"]["until"] > (now_ts or time.time())


def base_lock_status(st, now_ts=None):
    """只看手動鎖定／排程／番茄鐘（不含緊急使用、不含額度）。
    回傳 (locked, until, source)。是額度記帳「這段時間本來自不自由」的判準。"""
    now_ts = now_ts or time.time()
    candidates = []  # (結束時間, 來源)
    if st["lock_until"] > now_ts:
        candidates.append((st["lock_until"], "manual"))
    sched_end = active_schedule_end(st)
    if sched_end is not None and st["skip_until"] >= sched_end:
        sched_end = None  # 這個時段已被使用者跳過
    if sched_end:
        candidates.append((sched_end, "schedule"))
    phase, _, phase_end = pomo_status(st, now_ts)
    if phase == "focus":
        candidates.append((phase_end, "pomodoro"))
    if not candidates:
        return False, None, "none"
    until = max(ts for ts, _ in candidates)
    source = "+".join(src for _, src in candidates)
    return True, until, source


def _budget_effective(st, now_ts):
    """純函式，不寫入 state：算出目前有效的視窗起點／已用秒數／剩餘秒數／視窗結束時間。
    用最後一次記帳的檢查點（synced_at/used_seconds/was_free）往前推算，
    平常呼叫不需要寫檔，只有 sync_budget() 才會真的持久化。"""
    b = st["budget"]
    cap = b["minutes"] * 60
    window_s = b["window_hours"] * 3600
    ws = b["window_start"]
    if window_s <= 0:
        return ws, 0.0, cap, ws
    elapsed = now_ts - ws
    if elapsed >= window_s:
        ws = ws + math.floor(elapsed / window_s) * window_s
    used = 0.0 if ws != b["window_start"] else b["used_seconds"]
    if b.get("was_free"):
        free_since = max(b["synced_at"], ws)  # 視窗翻頁時，舊視窗的時間不算進新視窗
        used = min(cap, used + max(0.0, now_ts - free_since))
    remaining = max(0.0, cap - used)
    return ws, used, remaining, ws + window_s


def budget_locked(st, now_ts=None):
    """額度是否正在強制鎖定。回傳 (locked, remaining_seconds, window_end)。不寫入 state。"""
    now_ts = now_ts or time.time()
    b = st["budget"]
    if not b["enabled"]:
        return False, None, None
    base_locked, _, _ = base_lock_status(st, now_ts)
    _, _, remaining, window_end = _budget_effective(st, now_ts)
    return (not base_locked and remaining <= 0), remaining, window_end


def sync_budget(st, now_ts=None):
    """把額度記帳結果寫回 state。只有在視窗翻頁或「空閒⇄可用」狀態轉換時才會真的
    存檔——持續消耗額度的過程本身不寫檔，靠 _budget_effective() 用時間差推算即可。
    呼叫前需持有 state_lock。"""
    now_ts = now_ts or time.time()
    b = st["budget"]
    if not b["enabled"]:
        return
    ws, used, _, _ = _budget_effective(st, now_ts)
    base_locked, _, _ = base_lock_status(st, now_ts)
    now_free = not base_locked
    changed = (ws != b["window_start"]) or (now_free != b.get("was_free"))
    b["window_start"] = ws
    b["used_seconds"] = used
    b["synced_at"] = now_ts
    b["was_free"] = now_free
    if changed:
        save_state(st)


def lock_status(st, ignore_emergency=False):
    """回傳 (locked: bool, until: timestamp|None, source: str)。source 以 + 串接來源。
    緊急使用只「懸置」鎖定：底層封鎖期間（含額度）照常計算（ignore_emergency=True 可取得），
    所以次數歸零與到期自動上鎖都不受影響。"""
    now_ts = time.time()
    base_locked, base_until, base_source = base_lock_status(st, now_ts)
    if base_locked:
        locked, until, source = True, base_until, base_source
    else:
        bg_locked, _, window_end = budget_locked(st, now_ts)
        locked, until, source = (True, window_end, "budget") if bg_locked else (False, None, "none")
    if not locked:
        return False, None, "none"
    if not ignore_emergency and emergency_active(st, now_ts):
        return False, st["emergency"]["until"], "emergency"
    return True, until, source


# ---------- hosts 檔操作 ----------

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def normalize_site(raw):
    s = raw.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0].split("?")[0].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", s):
        return None
    return s


def build_block_lines(sites):
    # 導向 127.0.0.1，讓本機的封鎖提示頁（port 80）接住 http 請求
    lines = [MARK_START]
    for site in sites:
        lines.append(f"127.0.0.1 {site}")
        lines.append(f"127.0.0.1 www.{site}")
    lines.append(MARK_END)
    return lines


hosts_error = None  # 最近一次寫 hosts 的錯誤訊息，顯示在 UI


def apply_hosts(locked, sites):
    """把封鎖區塊寫入/移除 hosts。回傳是否有變更。"""
    global hosts_error
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
            content = f.read()
    except OSError as e:
        hosts_error = f"無法讀取 hosts：{e}"
        return False

    has_block = MARK_START in content
    need_block = locked and bool(sites)
    if not has_block and not need_block:
        hosts_error = None
        return False  # 沒有封鎖需求也沒有殘留區塊，不動 hosts

    # 移除舊區塊
    pattern = re.compile(
        re.escape(MARK_START) + r".*?" + re.escape(MARK_END) + r"\r?\n?",
        re.DOTALL,
    )
    cleaned = pattern.sub("", content).rstrip("\r\n")

    if locked and sites:
        new_content = cleaned + "\r\n" + "\r\n".join(build_block_lines(sites)) + "\r\n"
    else:
        new_content = cleaned + "\r\n"

    if new_content == content:
        hosts_error = None
        return False
    try:
        with open(HOSTS_PATH, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(new_content)
        hosts_error = None
    except PermissionError:
        hosts_error = "沒有權限寫入 hosts — 請用 start.bat（系統管理員）重新啟動"
        return False
    except OSError as e:
        hosts_error = f"寫入 hosts 失敗：{e}"
        return False

    subprocess.run(
        ["ipconfig", "/flushdns"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return True


# ---------- 封鎖提示頁 + 黑洞代理 ----------

block_page_error = None  # port 80 綁定失敗的訊息


def render_block_page(host):
    with state_lock:
        _, until, _ = lock_status(state)
    remain = ""
    if until:
        sec = max(0, int(until - time.time()))
        h, m = sec // 3600, sec % 3600 // 60
        remain = (f"{h} 小時 " if h else "") + f"{m} 分鐘後解鎖"
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>網站已封鎖</title>
<meta http-equiv="refresh" content="30"><!-- 解鎖後自動變回真正的網站 -->
<style>
  :root {{ --bg:#f7f7f5; --text:#1c1c1a; --dim:#6e6e68; --bad:#b3261e;
          --btn-bg:#1c1c1a; --btn-fg:#ffffff; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#151515; --text:#ebebe8; --dim:#a2a29b; --bad:#f08579;
            --btn-bg:#ebebe8; --btn-fg:#151515; }}
  }}
  body {{ background:var(--bg); color:var(--text);
         font-family:"Segoe UI","Microsoft JhengHei",system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .box {{ text-align:center; padding:40px 24px; max-width:480px; }}
  h1 {{ font-size:22px; font-weight:500; margin:0 0 8px; }}
  .host {{ color:var(--bad); }}
  .remain {{ color:var(--dim); font-size:14px; margin-bottom:28px; }}
  a.btn {{ display:inline-block; background:var(--btn-bg); color:var(--btn-fg); text-decoration:none;
          font-size:15px; font-weight:500; padding:12px 28px; border-radius:8px; }}
  a.small {{ display:block; margin-top:24px; color:var(--dim); font-size:12px; text-decoration:none; }}
</style></head>
<body><div class="box">
  <h1><span class="host">{host}</span> 已被封鎖</h1>
  <div class="remain">{remain or "專心時間進行中"}</div>
  <a class="btn" href="{HEPTABASE_URL}">前往 Heptabase 寫筆記</a>
  <a class="small" href="http://localhost:{PORT}">Digital Detox 控制台</a>
</div></body></html>"""


class BlockPageHandler(BaseHTTPRequestHandler):
    """任何請求都回封鎖頁；CONNECT（https 代理隧道）一律拒絕。"""

    def _host(self):
        h = self.headers.get("Host", "")
        if self.path.startswith("http"):  # 代理模式的絕對網址
            m = re.match(r"https?://([^/]+)", self.path)
            if m:
                h = m.group(1)
        return h.split(":")[0] or "此網站"

    def _serve_page(self):
        record_blocked_host(self._host())
        body = render_block_page(self._host()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = _serve_page

    def do_CONNECT(self):
        record_blocked_host(self.path.split(":")[0])  # https 隧道請求的目標主機
        self.send_response(403)
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, *args):
        pass


recent_blocked = {}  # host -> 最後一次被擋的 timestamp（供白名單除錯用，不落地寫檔）
BLOCKED_LOG_CAP = 200


def record_blocked_host(host):
    if not host:
        return
    recent_blocked[host] = time.time()
    if len(recent_blocked) > BLOCKED_LOG_CAP:
        oldest = min(recent_blocked, key=recent_blocked.get)
        del recent_blocked[oldest]


def start_block_server(port):
    global block_page_error
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), BlockPageHandler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError as e:
        if port == BLOCK_PAGE_PORT:
            block_page_error = f"埠 {port} 無法使用（{e.strerror or e}），被封網站將顯示連線錯誤而非提示頁"


# ---------- 全部封鎖模式（系統 Proxy PAC）----------

REG_INET = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
PAC_URL = f"http://127.0.0.1:{PORT}/proxy.pac"


# 服務伴隨網域：放行某服務時自動放行它必需的 CDN/後端，服務才真的能用。
# 只放「該服務不可或缺、且無法單獨拿來瀏覽被封鎖網站」的網域。
# 以 "=" 開頭者為「精確主機」：只放行該主機本身，不含其他子網域。用於
# 服務所需、但掛在被封鎖網域底下的端點（放行整個網域會讓封鎖失效）。
COMPANION_DOMAINS = {
    "messenger.com": [
        "fbcdn.net",                       # Messenger 網頁版的 JS/圖片都在 Meta CDN 上
        "=web-chat-e2ee.facebook.com",     # 端對端加密聊天內容（facebook.com 本體仍封鎖）
        "=edge-chat.facebook.com",         # 即時訊息長連線
    ],
    "claude.ai": ["anthropic.com", "claudeusercontent.com"],
    "spotify.com": ["scdn.co", "spotifycdn.com"],  # 音訊串流與封面圖 CDN
    # 分享/資源網域 + 內建客服（Intercom）+ 自動更新（GitHub Releases）
    "heptabase.com": ["hepta.so", "intercom.io", "intercomcdn.com",
                      "github.com", "githubusercontent.com"],
    # 只放行搜尋/AI 助理實際用到的兩個主機（皆為精確主機）。
    # 刻意不放行 gstatic.com（圖片縮圖伺服器，避免被封鎖網站的圖片透過搜尋結果洩漏）、
    # 不放行 translate.google(apis).com（翻譯服務可整頁代理顯示被封鎖網站，是已知繞道手法）、
    # 也不整域放行 google.com（避免連 Gmail/雲端硬碟/日曆/地圖/相簿一起解鎖）。
    "google.com": ["=www.google.com", "=www.googleapis.com"],
}

# 使用者輸入這些網域加入白名單時，不整域放行（不會產生 *.domain 萬用規則），
# 只依 COMPANION_DOMAINS 裡列出的精確主機生效。用於本體網域過於龐大、
# 整域放行會牽動太多不相關服務的情況（例如 google.com 底下掛了整套 Google 帳號服務）。
RESTRICTED_ROOT_DOMAINS = {"google.com"}


def expand_allow_sites(allow_sites):
    seen = list(allow_sites)
    for s in allow_sites:
        for extra in COMPANION_DOMAINS.get(s, []):
            if extra not in seen:
                seen.append(extra)
    return seen


def build_pac(allow_sites):
    rules = ""
    for s in expand_allow_sites(allow_sites):
        if s.startswith("="):  # 精確主機，不放行其子網域
            rules += f'  if (host === "{s[1:]}") return "DIRECT";\n'
        elif s in RESTRICTED_ROOT_DOMAINS:
            continue  # 不整域放行，只靠上面該網域列出的精確主機 companion 生效
        else:
            rules += f'  if (host === "{s}" || shExpMatch(host, "*.{s}")) return "DIRECT";\n'
    return (
        "function FindProxyForURL(url, host) {\n"
        "  host = host.toLowerCase();\n"
        '  if (host === "localhost" || host === "127.0.0.1" || shExpMatch(host, "*.local")) return "DIRECT";\n'
        + rules +
        f'  return "PROXY 127.0.0.1:{PROXY_PORT}";\n'
        "}\n"
    )


def _refresh_wininet():
    try:
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
    except Exception:
        pass


_last_pac = None  # 上次生效的 PAC 內容；內容變了（如白名單增減）也要廣播，瀏覽器才會重抓


def apply_pac(on):
    """設定/移除系統代理自動設定（HKCU，不需管理員）。冪等。
    呼叫前需持有 state_lock（會讀 allow_sites）。"""
    global _last_pac
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_INET, 0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        )
    except OSError:
        return
    try:
        try:
            current = winreg.QueryValueEx(key, "AutoConfigURL")[0]
        except FileNotFoundError:
            current = None
        if on:
            pac = build_pac(state["allow_sites"])
            url_changed = current != PAC_URL
            if url_changed:
                winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, PAC_URL)
            if url_changed or pac != _last_pac:
                _last_pac = pac
                _refresh_wininet()
        elif current == PAC_URL:
            winreg.DeleteValue(key, "AutoConfigURL")
            _last_pac = None
            _refresh_wininet()
    finally:
        winreg.CloseKey(key)


# ---------- 開機自動啟動（Windows 工作排程器）----------

TASK_NAME = "DigitalDetox"
APP_PATH = os.path.abspath(__file__)
autostart_cache = None  # 避免每次輪詢都 spawn schtasks


def _pythonw():
    """回傳無主控台版的直譯器路徑（開機時不彈黑窗）。"""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def _schtasks(args):
    try:
        return subprocess.run(
            ["schtasks"] + args, capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).returncode == 0
    except Exception:
        return False


def get_autostart():
    global autostart_cache
    if autostart_cache is None:
        autostart_cache = _schtasks(["/query", "/tn", TASK_NAME])
    return autostart_cache


def set_autostart(on):
    """建立/移除「登入時以最高權限執行」的排程工作。需管理員權限。"""
    global autostart_cache
    if on:
        cmd = f'"{_pythonw()}" "{APP_PATH}"'
        ok = _schtasks(["/create", "/tn", TASK_NAME, "/tr", cmd,
                        "/sc", "onlogon", "/rl", "highest", "/f"])
    else:
        ok = _schtasks(["/delete", "/tn", TASK_NAME, "/f"])
    if ok:
        autostart_cache = on
    return ok


WAKE = threading.Event()  # API 改動狀態時喚醒 enforcer，平時讓它長睡省電


def sync_emergency_period():
    """偵測封鎖期間的起訖，於新的封鎖期間開始時把緊急使用次數歸零。
    以底層鎖定狀態（忽略緊急使用）判斷，故緊急使用中不會被誤判成期間結束。
    armed 存在 state.json，重啟不會白白多送次數。呼叫前需持有 state_lock。"""
    base_locked = lock_status(state, ignore_emergency=True)[0]
    em = state["emergency"]
    if base_locked == em["armed"]:
        return
    em.update({"armed": base_locked, "used": 0, "until": 0})
    save_state(state)


def enforcer_loop():
    """該鎖就鎖、該解就解。省電設計：睡到「下一個狀態邊界」（排程開始/結束、
    鎖定到期、番茄鐘換階段），無事最多 120 秒醒一次（兼防手動竄改 hosts 與
    系統休眠期間漏掉邊界）。API 改動會用 WAKE 立即喚醒。"""
    while True:
        with state_lock:
            if state.get("pomo") and pomo_status(state)[0] is None:
                state["pomo"] = None  # 番茄鐘已跑完，清掉
                save_state(state)
            sync_emergency_period()
            sync_budget(state)
            locked, _, _ = lock_status(state)
            apply_hosts(locked, state["sites"])
            apply_pac(locked and state["block_all"])
            wait_s = seconds_to_next_change(state)
        timeout = 120.0 if wait_s is None else max(1.0, min(wait_s + 1.0, 120.0))
        WAKE.wait(timeout)
        WAKE.clear()


# ---------- API ----------

def body():
    """安全取得 JSON 請求內容；非 JSON 或空 body 回 {}，避免打出 500。"""
    return request.get_json(silent=True) or {}


def pomo_payload():
    p = state.get("pomo")
    phase, cycle, phase_end = pomo_status(state)
    if not p or phase is None:
        return None
    return {"phase": phase, "cycle": cycle, "cycles": p["cycles"],
            "phase_end": phase_end, "focus": p["focus"], "break": p["break"]}


def budget_payload():
    b = state["budget"]
    if not b["enabled"]:
        return {"enabled": False}
    now = time.time()
    _, used, remaining, window_end = _budget_effective(state, now)
    locked, _, _ = budget_locked(state, now)
    return {
        "enabled": True,
        "window_hours": b["window_hours"],
        "minutes": b["minutes"],
        "used_seconds": used,
        "remaining_seconds": remaining,
        "window_end": window_end,
        "locked_by_budget": locked,
    }


def state_payload():
    locked, until, source = lock_status(state)
    return {
        "build": APP_BUILD,
        "pomo": pomo_payload(),
        "budget": budget_payload(),
        "emergency": {
            "active": emergency_active(state),
            "until": state["emergency"]["until"] or None,
            "left": emergency_left(state),
            "max": EMERGENCY_MAX,
            "minutes": EMERGENCY_MINUTES,
            "available": lock_status(state, ignore_emergency=True)[0],
        },
        "locked": locked,
        "until": until,
        "source": source,
        "strict": state["strict"],
        "sites": state["sites"],
        "schedules": state["schedules"],
        "block_all": state["block_all"],
        "allow_sites": state["allow_sites"],
        "admin": is_admin(),
        "autostart": get_autostart(),
        "hosts_error": hosts_error,
        "block_page_error": block_page_error,
        "now": time.time(),
    }


def enforce_now():
    """依目前狀態立刻套用 hosts 與系統代理。呼叫前需持有 state_lock。"""
    sync_budget(state)
    locked, _, _ = lock_status(state)
    apply_hosts(locked, state["sites"])
    apply_pac(locked and state["block_all"])
    WAKE.set()  # 讓 enforcer 依新狀態重算下一次喚醒時間


@app.get("/api/state")
def api_state():
    with state_lock:
        return jsonify(state_payload())


@app.post("/api/lock")
def api_lock():
    try:
        minutes = int(body().get("minutes", 0))
    except (TypeError, ValueError):
        minutes = 0
    if not 1 <= minutes <= 24 * 60:
        return jsonify({"error": "分鐘數需在 1–1440 之間"}), 400
    with state_lock:
        state["lock_until"] = max(state["lock_until"], time.time() + minutes * 60)
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/unlock")
def api_unlock():
    with state_lock:
        locked, _, _ = lock_status(state)
        if locked and state["strict"]:
            return jsonify({"error": "嚴格模式鎖定中，無法提前解除"}), 403
        state["lock_until"] = 0
        state["pomo"] = None  # 解除鎖定同時停掉番茄鐘，否則專注時段會繼續鎖
        sched_end = active_schedule_end(state)
        if sched_end:
            state["skip_until"] = sched_end  # 跳過目前這個排程時段
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/sites")
def api_add_site():
    site = normalize_site(body().get("site", ""))
    if not site:
        return jsonify({"error": "網址格式不正確，例如：youtube.com"}), 400
    with state_lock:
        if site not in state["sites"]:
            state["sites"].append(site)
            save_state(state)
            locked, _, _ = lock_status(state)
            apply_hosts(locked, state["sites"])
        return jsonify(state_payload())


@app.post("/api/sites/remove")
def api_remove_site():
    site = body().get("site", "")
    with state_lock:
        locked, _, _ = lock_status(state)
        if locked and state["strict"]:
            return jsonify({"error": "嚴格模式鎖定中，無法移除網站"}), 403
        if site in state["sites"]:
            state["sites"].remove(site)
            save_state(state)
            apply_hosts(locked, state["sites"])
        return jsonify(state_payload())


@app.post("/api/schedules")
def api_add_schedule():
    data = body()
    days = sorted(set(int(d) for d in data.get("days", []) if 0 <= int(d) <= 6))
    start, end = data.get("start", ""), data.get("end", "")
    if not days:
        return jsonify({"error": "請至少選一天"}), 400
    if not re.fullmatch(r"\d{2}:\d{2}", start) or not re.fullmatch(r"\d{2}:\d{2}", end):
        return jsonify({"error": "時間格式錯誤"}), 400
    if start == end:
        return jsonify({"error": "開始與結束時間不能相同"}), 400
    with state_lock:
        state["schedules"].append(
            {"id": state["next_id"], "days": days, "start": start, "end": end}
        )
        state["next_id"] += 1
        state["skip_until"] = 0
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/schedules/remove")
def api_remove_schedule():
    try:
        sid = int(body().get("id", -1))
    except (TypeError, ValueError):
        sid = -1
    with state_lock:
        locked, _, _ = lock_status(state)
        if locked and state["strict"]:
            return jsonify({"error": "嚴格模式鎖定中，無法刪除排程"}), 403
        state["schedules"] = [s for s in state["schedules"] if s["id"] != sid]
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/strict")
def api_strict():
    on = bool(body().get("on"))
    with state_lock:
        locked, _, _ = lock_status(state)
        if not on and locked and state["strict"]:
            return jsonify({"error": "鎖定期間無法關閉嚴格模式"}), 403
        state["strict"] = on
        save_state(state)
        return jsonify(state_payload())


@app.post("/api/block_all")
def api_block_all():
    on = bool(body().get("on"))
    with state_lock:
        locked, _, _ = lock_status(state)
        if not on and locked and state["strict"]:
            return jsonify({"error": "嚴格模式鎖定中，無法關閉全部封鎖"}), 403
        state["block_all"] = on
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/allow")
def api_add_allow():
    site = normalize_site(body().get("site", ""))
    if not site:
        return jsonify({"error": "網址格式不正確，例如：heptabase.com"}), 400
    with state_lock:
        locked, _, _ = lock_status(state)
        if locked and state["strict"] and state["block_all"]:
            return jsonify({"error": "嚴格模式鎖定中，無法新增白名單"}), 403
        if site not in state["allow_sites"]:
            state["allow_sites"].append(site)
            save_state(state)
        return jsonify(state_payload())


@app.post("/api/allow/remove")
def api_remove_allow():
    site = body().get("site", "")
    with state_lock:
        if site in state["allow_sites"]:
            state["allow_sites"].remove(site)
            save_state(state)
        return jsonify(state_payload())


@app.post("/api/emergency")
def api_emergency():
    """緊急使用：暫時解除鎖定 EMERGENCY_MINUTES 分鐘。嚴格模式下仍可用——
    這是刻意保留的安全閥（次數與時長有限，不會讓嚴格模式失效）。"""
    with state_lock:
        sync_emergency_period()
        if not lock_status(state, ignore_emergency=True)[0]:
            return jsonify({"error": "目前不在封鎖期間，不需要緊急使用"}), 400
        em = state["emergency"]
        if emergency_active(state):
            return jsonify({"error": "緊急使用進行中"}), 400
        if emergency_left(state) <= 0:
            return jsonify({"error": f"本次封鎖期間的 {EMERGENCY_MAX} 次緊急使用已用完"}), 403
        em["used"] += 1
        em["until"] = time.time() + EMERGENCY_MINUTES * 60
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/budget/config")
def api_budget_config():
    """設定/停用使用額度。任何變更都會重新起算目前視窗（含關閉時的既有進度）。
    鎖定中若開嚴格模式，任何額度設定變更一律禁止——跟 sites/schedules 一致，
    不分放寬/收緊方向（比 block_all 的「只擋放寬」規則簡單，範圍更保守）。"""
    b = body()
    with state_lock:
        locked, _, _ = lock_status(state)
        if locked and state["strict"]:
            return jsonify({"error": "嚴格模式鎖定中，無法變更額度設定"}), 403
        enabled = bool(b.get("enabled"))
        try:
            window_hours = float(b.get("window_hours", 0))
            minutes = float(b.get("minutes", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "數字格式錯誤"}), 400
        if enabled:
            if not (BUDGET_MIN_WINDOW_HOURS <= window_hours <= BUDGET_MAX_WINDOW_HOURS):
                return jsonify({"error": "視窗長度需在 0.5 小時–30 天之間"}), 400
            if not (1 <= minutes <= window_hours * 60):
                return jsonify({"error": "分鐘數需在 1 到視窗長度之間"}), 400
        now = time.time()
        state["budget"] = {
            "enabled": enabled, "window_hours": window_hours, "minutes": minutes,
            "window_start": now, "used_seconds": 0.0, "synced_at": now,
            "was_free": not base_lock_status(state, now)[0],
        }
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/pomo/start")
def api_pomo_start():
    b = body()
    try:
        focus = int(b.get("focus", 25))
        brk = int(b.get("break", 5))
        cycles = int(b.get("cycles", 4))
    except (TypeError, ValueError):
        return jsonify({"error": "數字格式錯誤"}), 400
    if not (1 <= focus <= 180 and 1 <= brk <= 60 and 1 <= cycles <= 12):
        return jsonify({"error": "範圍：專注 1–180 分、休息 1–60 分、1–12 顆"}), 400
    with state_lock:
        state["pomo"] = {"start": time.time(), "focus": focus, "break": brk, "cycles": cycles}
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/pomo/stop")
def api_pomo_stop():
    with state_lock:
        phase, _, _ = pomo_status(state)
        if phase and state["strict"]:
            return jsonify({"error": "嚴格模式進行中，無法中止番茄鐘"}), 403
        state["pomo"] = None
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/autostart")
def api_autostart():
    on = bool(body().get("on"))
    if not is_admin():
        return jsonify({"error": "需要管理員權限才能設定開機自動啟動"}), 403
    if not set_autostart(on):
        return jsonify({"error": "設定失敗（工作排程器拒絕，請確認管理員權限）"}), 500
    with state_lock:
        return jsonify(state_payload())


@app.get("/api/blocked_recent")
def api_blocked_recent():
    """診斷用：全部封鎖模式下最近被擋的網域（不落地寫檔，僅存於記憶體）。"""
    items = sorted(recent_blocked.items(), key=lambda kv: -kv[1])
    return jsonify([{"host": h, "seconds_ago": round(time.time() - t)} for h, t in items[:50]])


@app.get("/proxy.pac")
def proxy_pac():
    with state_lock:
        pac = build_pac(state["allow_sites"])
    return pac, 200, {
        "Content-Type": "application/x-ns-proxy-autoconfig",
        "Cache-Control": "no-store, max-age=0",  # 白名單改了要立刻生效，禁止快取 PAC
    }


@app.get("/blocked")
def blocked_preview():
    return render_block_page(request.args.get("site", "example.com"))


@app.get("/")
def index():
    # no-store：改版後開著的分頁不會停在舊 HTML（舊頁面的按鈕會點了沒反應）
    return PAGE.replace("__BUILD__", APP_BUILD), 200, {"Cache-Control": "no-store, max-age=0"}


# ---------- 前端頁面 ----------

PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Digital Detox</title>
<style>
  :root {
    --bg: #f7f7f5; --card: #ffffff; --line: #e4e4e0; --line2: #c9c9c3;
    --text: #1c1c1a; --dim: #6e6e68;
    --ok: #15734f; --bad: #b3261e;
    --warn: #7a4b0a; --warn-bg: #faeeda;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #151515; --card: #1e1e1e; --line: #333330; --line2: #4c4c47;
      --text: #ebebe8; --dim: #a2a29b;
      --ok: #59c99a; --bad: #f08579;
      --warn: #e8b35c; --warn-bg: #3a2d14;
    }
  }
  :root[data-theme="dark"] {
    --bg: #151515; --card: #1e1e1e; --line: #333330; --line2: #4c4c47;
    --text: #ebebe8; --dim: #a2a29b;
    --ok: #59c99a; --bad: #f08579;
    --warn: #e8b35c; --warn-bg: #3a2d14;
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", "Microsoft JhengHei", system-ui, sans-serif;
    margin: 0; padding: 24px clamp(16px, 3vw, 40px) 60px;
  }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  .cols { columns: 380px; column-gap: 14px; }
  .cols .card { break-inside: avoid; }
  .status { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; }
  h1 { font-size: 20px; font-weight: 500; margin-bottom: 2px; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 18px; margin-bottom: 14px;
  }
  .card h2 { font-size: 13px; color: var(--dim); font-weight: 500; margin-bottom: 12px; }
  #status-big { font-size: 24px; font-weight: 500; }
  #status-big.locked { color: var(--bad); }
  #status-big.free { color: var(--ok); }
  #status-big.emg { color: var(--warn); }
  #countdown { font-size: 14px; color: var(--dim); margin-top: 4px; }
  .banner {
    border-radius: 8px; padding: 10px 14px; font-size: 13px;
    margin-bottom: 14px; display: none;
  }
  .banner.show { display: block; }
  .banner.warn { background: var(--warn-bg); color: var(--warn); }
  button {
    background: transparent; color: var(--text); border: 1px solid var(--line2);
    border-radius: 8px; padding: 8px 16px; font-size: 14px; font-weight: 500;
    cursor: pointer; font-family: inherit;
  }
  button:hover:not(:disabled) { background: var(--bg); }
  button.ghost { color: var(--dim); border-color: var(--line) }
  button.danger { color: var(--bad); border-color: var(--bad); }
  button:disabled { opacity: .4; cursor: not-allowed; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input[type=text], input[type=number], input[type=time] {
    background: var(--card); border: 1px solid var(--line2); color: var(--text);
    border-radius: 8px; padding: 8px 12px; font-size: 14px; font-family: inherit;
  }
  input:focus, button:focus-visible { outline: 2px solid var(--dim); outline-offset: 1px; }
  input[type=number] { width: 80px; }
  ul { list-style: none; margin-top: 10px; }
  li {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 4px; border-bottom: 1px solid var(--line); font-size: 14px;
  }
  li:last-child { border-bottom: 0; }
  li button { padding: 4px 10px; font-size: 12px; flex-shrink: 0; margin-left: 8px; }
  .days label {
    display: inline-block; padding: 5px 10px; border: 1px solid var(--line);
    border-radius: 7px; font-size: 13px; cursor: pointer; user-select: none;
    color: var(--dim);
  }
  .days label:has(input:checked) { border-color: var(--text); }
  .days input { display: none; }
  .days input:checked + span { color: var(--text); font-weight: 500; }
  .switch { display: flex; align-items: flex-start; gap: 10px; font-size: 14px; }
  .switch input { margin-top: 3px; flex-shrink: 0; }
  #msg { font-size: 13px; min-height: 18px; margin: -4px 0 12px 2px; }
  #msg.err { color: var(--bad); }
  #msg.ok { color: var(--ok); }
  a { color: var(--text); }
</style>
</head>
<body>
  <div class="head">
    <div>
      <h1>Digital Detox</h1>
      <div class="sub">封鎖分心網站 · 對所有瀏覽器生效（hosts 層級）</div>
    </div>
    <button class="ghost" id="theme-btn" onclick="cycleTheme()">主題：自動</button>
  </div>

  <div id="admin-banner" class="banner warn">
    目前不是以系統管理員執行，無法寫入 hosts 檔 — 請關閉後用 <b>start.bat</b> 啟動。
  </div>
  <div id="hosts-banner" class="banner warn"></div>
  <div id="blockpage-banner" class="banner warn"></div>

  <div class="card status">
    <div id="status-big" class="free">—</div>
    <div id="countdown"></div>
  </div>

  <div id="msg"></div>

  <div class="cols">
  <div class="card">
    <h2>立即鎖定</h2>
    <div class="row">
      <button onclick="lock(25)">25 分鐘</button>
      <button onclick="lock(60)">1 小時</button>
      <button onclick="lock(180)">3 小時</button>
      <input type="number" id="custom-min" min="1" max="1440" placeholder="分鐘">
      <button class="ghost" onclick="lockCustom()">自訂</button>
      <button class="danger" id="unlock-btn" onclick="unlock()">解除鎖定</button>
    </div>
    <label class="switch" style="margin-top:12px">
      <input type="checkbox" id="blockall-toggle" onchange="setBlockAll(this.checked)">
      <span><b>全部封鎖</b>：鎖定時封鎖所有網站，只有下方白名單可以連</span>
    </label>
  </div>

  <div class="card">
    <h2>緊急使用</h2>
    <div class="row" id="emg-idle">
      <button class="ghost" id="emg-btn" onclick="armEmergency()">緊急使用 5 分鐘</button>
      <span id="emg-info" style="color:var(--dim); font-size:14px"></span>
    </div>
    <div class="row" id="emg-confirm" style="display:none">
      <span style="flex:1; font-size:14px">確定用掉 1 次？解除 5 分鐘後自動鎖回去。</span>
      <button class="danger" onclick="doEmergency()">確定使用</button>
      <button class="ghost" onclick="cancelEmergency()">取消</button>
    </div>
    <div class="sub" style="margin:8px 0 0">每次封鎖期間 2 次，嚴格模式下仍可用；時間到自動鎖回去</div>
  </div>

  <div class="card">
    <h2>番茄鐘</h2>
    <div class="row" id="pomo-setup">
      <span style="color:var(--dim)">專注</span>
      <input type="number" id="pomo-focus" min="1" max="180" value="25">
      <span style="color:var(--dim)">分，休息</span>
      <input type="number" id="pomo-break" min="1" max="60" value="5">
      <span style="color:var(--dim)">分 ×</span>
      <input type="number" id="pomo-cycles" min="1" max="12" value="4">
      <span style="color:var(--dim)">顆</span>
      <button onclick="startPomo()">開始</button>
    </div>
    <div class="row" id="pomo-status" style="display:none">
      <span id="pomo-info" style="font-size:15px; flex:1"></span>
      <button class="danger" id="pomo-stop" onclick="stopPomo()">中止</button>
    </div>
    <div class="sub" style="margin:8px 0 0">專注時段鎖定封鎖清單，休息時段自動解鎖</div>
  </div>

  <div class="card">
    <h2>使用額度</h2>
    <label class="switch">
      <input type="checkbox" id="budget-toggle">
      啟用額度限制
    </label>
    <div class="row" style="margin-top:10px">
      <span style="color:var(--dim)">每</span>
      <input type="number" id="budget-window-val" min="1" value="3" style="width:70px">
      <select id="budget-window-unit">
        <option value="hours">小時</option>
        <option value="days">天</option>
      </select>
      <span style="color:var(--dim)">可用</span>
      <input type="number" id="budget-minutes" min="1" value="30" style="width:80px">
      <span style="color:var(--dim)">分鐘</span>
      <button id="budget-save-btn" onclick="saveBudget()">儲存</button>
    </div>
    <div class="sub" id="budget-info" style="margin-top:8px"></div>
    <div class="sub" style="margin-top:4px">
      平常照舊，只在「原本不會被鎖」的時段計時；額度用完在視窗剩餘時間內強制鎖定，過視窗自動重置
    </div>
  </div>

  <div class="card">
    <h2>白名單（全部封鎖時仍可連線，含子網域）</h2>
    <div class="sub" style="margin:0 0 8px">已知服務會自動放行必需的 CDN（如 Messenger 的 fbcdn.net），不影響其他封鎖</div>
    <div class="row">
      <input type="text" id="new-allow" placeholder="例如 heptabase.com" style="flex:1"
             onkeydown="if(event.key==='Enter')addAllow()">
      <button onclick="addAllow()">加入</button>
    </div>
    <ul id="allow-list"></ul>
  </div>

  <div class="card">
    <h2>封鎖清單</h2>
    <div class="row">
      <input type="text" id="new-site" placeholder="例如 youtube.com" style="flex:1"
             onkeydown="if(event.key==='Enter')addSite()">
      <button onclick="addSite()">加入</button>
    </div>
    <ul id="site-list"></ul>
  </div>

  <div class="card">
    <h2>定時鎖定排程</h2>
    <div class="days row" id="day-picker"></div>
    <div class="row" style="margin-top:10px">
      <input type="time" id="sch-start" value="09:00">
      <span style="color:var(--dim)">到</span>
      <input type="time" id="sch-end" value="12:00">
      <button onclick="addSchedule()">新增排程</button>
    </div>
    <ul id="sch-list"></ul>
  </div>

  <div class="card">
    <h2>嚴格模式</h2>
    <label class="switch">
      <input type="checkbox" id="strict-toggle" onchange="setStrict(this.checked)">
      <span>鎖定期間<b>不能</b>提前解除、不能移除網站或刪除排程</span>
    </label>
  </div>

  <div class="card">
    <h2>開機自動啟動</h2>
    <label class="switch">
      <input type="checkbox" id="autostart-toggle" onchange="setAutostart(this.checked)">
      <span>登入 Windows 時自動在背景啟動（以最高權限執行，<b>不會</b>每次跳 UAC）</span>
    </label>
    <div class="sub" id="autostart-note" style="margin:8px 0 0"></div>
  </div>

  </div>

  <div class="sub" style="margin-top:8px">
    被封鎖的網站會顯示提示頁並可一鍵前往 Heptabase（<a href="/blocked?site=youtube.com"
    target="_blank" style="color:var(--accent)">預覽提示頁</a>）
    · 版本 <span id="build-tag">—</span>
  </div>

<script>
const DAY_NAMES = ["一","二","三","四","五","六","日"];
let S = null;

const THEMES = ["auto", "light", "dark"];
const THEME_NAMES = {auto: "自動", light: "淺色", dark: "深色"};
function applyTheme() {
  const t = localStorage.theme || "auto";
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  document.getElementById("theme-btn").textContent = "主題：" + THEME_NAMES[t];
}
function cycleTheme() {
  const t = localStorage.theme || "auto";
  localStorage.theme = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
  applyTheme();
}
applyTheme();

const picker = document.getElementById("day-picker");
DAY_NAMES.forEach((n, i) => {
  picker.insertAdjacentHTML("beforeend",
    `<label><input type="checkbox" value="${i}"><span>週${n}</span></label>`);
});

function flash(text, ok) {
  const el = document.getElementById("msg");
  el.textContent = text;
  el.className = ok ? "ok" : "err";
}

async function api(path, body) {
  const opt = body === undefined ? {} :
    { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json();
  flash(r.ok ? "" : (data.error || "發生錯誤"), false);
  if (r.ok) { S = data; render(); }
  return r.ok;
}

function fmtRemain(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + " 小時 " : "") + (m ? m + " 分 " : "") + s + " 秒";
}

let clockOffset = 0;
function render() {
  if (!S) return;
  clockOffset = S.now - Date.now() / 1000;
  document.getElementById("build-tag").textContent = BUILD;
  const big = document.getElementById("status-big");
  if (S.emergency && S.emergency.active) {
    big.textContent = "緊急使用中";
    big.className = "emg";
  } else if (S.locked) {
    big.textContent = "鎖定中";
    big.className = "locked";
  } else {
    big.textContent = "未鎖定";
    big.className = "free";
  }
  tick();
  document.getElementById("admin-banner").classList.toggle("show", !S.admin);
  const hb = document.getElementById("hosts-banner");
  hb.textContent = S.hosts_error || "";
  hb.classList.toggle("show", !!S.hosts_error);
  const bb = document.getElementById("blockpage-banner");
  bb.textContent = S.block_page_error || "";
  bb.classList.toggle("show", !!S.block_page_error);
  document.getElementById("unlock-btn").disabled = !S.locked || (S.locked && S.strict);
  document.getElementById("strict-toggle").checked = S.strict;
  document.getElementById("blockall-toggle").checked = S.block_all;
  const as = document.getElementById("autostart-toggle");
  as.checked = S.autostart; as.disabled = !S.admin;
  document.getElementById("autostart-note").textContent =
    S.admin ? "" : "需以系統管理員執行才能設定";

  const E = S.emergency;
  const emgUsable = E.available && !E.active && E.left > 0;
  if (!emgUsable) emgArming = false;
  document.getElementById("emg-idle").style.display = emgArming ? "none" : "flex";
  document.getElementById("emg-confirm").style.display = emgArming ? "flex" : "none";
  document.getElementById("emg-btn").disabled = !emgUsable;
  document.getElementById("emg-info").textContent =
    !E.available ? "目前不在封鎖期間" :
    E.active ? "" :                                  // 倒數由 tick() 更新
    E.left > 0 ? `本次封鎖期間還可用 ${E.left}/${E.max} 次` : "本次封鎖期間已用完";

  const pomoActive = !!S.pomo;
  document.getElementById("pomo-setup").style.display = pomoActive ? "none" : "flex";
  document.getElementById("pomo-status").style.display = pomoActive ? "flex" : "none";
  if (pomoActive) document.getElementById("pomo-stop").disabled = S.strict;

  if (!budgetFormInit) { populateBudgetForm(); budgetFormInit = true; }
  const budgetLockedStrict = S.locked && S.strict;
  ["budget-toggle", "budget-window-val", "budget-window-unit", "budget-minutes", "budget-save-btn"]
    .forEach(id => { document.getElementById(id).disabled = budgetLockedStrict; });

  const allowUl = document.getElementById("allow-list");
  allowUl.innerHTML = "";
  S.allow_sites.forEach(site => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${site}</span>`;
    const btn = document.createElement("button");
    btn.className = "ghost"; btn.textContent = "移除";
    btn.onclick = () => api("/api/allow/remove", {site});
    li.appendChild(btn);
    allowUl.appendChild(li);
  });

  const siteUl = document.getElementById("site-list");
  siteUl.innerHTML = "";
  S.sites.forEach(site => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${site}</span>`;
    const btn = document.createElement("button");
    btn.className = "ghost"; btn.textContent = "移除";
    btn.disabled = S.locked && S.strict;
    btn.onclick = () => api("/api/sites/remove", {site});
    li.appendChild(btn);
    siteUl.appendChild(li);
  });

  const schUl = document.getElementById("sch-list");
  schUl.innerHTML = "";
  S.schedules.forEach(sch => {
    const days = sch.days.map(d => "週" + DAY_NAMES[d]).join("、");
    const li = document.createElement("li");
    li.innerHTML = `<span>${days} &nbsp; ${sch.start}–${sch.end}</span>`;
    const btn = document.createElement("button");
    btn.className = "ghost"; btn.textContent = "刪除";
    btn.disabled = S.locked && S.strict;
    btn.onclick = () => api("/api/schedules/remove", {id: sch.id});
    li.appendChild(btn);
    schUl.appendChild(li);
  });

  schedulePolling();  // 狀態變了 → 重排輪詢節奏（省電）
}

// ─── 省電輪詢：背景分頁完全停止；沒有倒數就不每秒重繪 ───
let tickTimer = null, refreshTimer = null;
function schedulePolling() {
  clearInterval(tickTimer); clearInterval(refreshTimer);
  tickTimer = refreshTimer = null;
  if (document.hidden) return;                          // 分頁在背景：全部停
  const counting = S && (S.locked || S.pomo || (S.emergency && S.emergency.active));
  if (counting) tickTimer = setInterval(tick, 1000);    // 只有倒數畫面需要每秒更新
  refreshTimer = setInterval(refresh, counting ? 10000 : 30000);
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) schedulePolling();               // 進背景：停止輪詢
  else refresh();                                       // 回前景：立即同步並重啟輪詢
});

const SRC_NAMES = {manual: "手動", schedule: "排程", pomodoro: "番茄鐘", budget: "額度"};
function srcLabel(source) {
  if (!source || source === "none" || source === "manual") return "";
  return "（" + source.split("+").map(s => SRC_NAMES[s] || s).join("＋") + "）";
}

function tick() {
  const el = document.getElementById("countdown");
  const emgActive = S && S.emergency && S.emergency.active && S.emergency.until;
  if (emgActive) {
    const er = S.emergency.until - (Date.now() / 1000 + clockOffset);
    if (er <= 0) { refresh(); return; }
    el.textContent = "緊急使用中 · " + fmtRemain(er) + " 後自動鎖回去";
    document.getElementById("emg-info").textContent = "剩 " + fmtRemain(er);
  } else if (S && S.locked && S.until) {
    const remain = S.until - (Date.now() / 1000 + clockOffset);
    if (remain <= 0) { refresh(); return; }
    el.textContent = "剩餘 " + fmtRemain(remain) + " " + srcLabel(S.source);
  } else {
    el.textContent = S && S.schedules.length ? "等待下一個排程時段" : "";
  }
  if (S && S.pomo) {
    const pr = S.pomo.phase_end - (Date.now() / 1000 + clockOffset);
    if (pr <= 0) { refresh(); return; }
    document.getElementById("pomo-info").textContent =
      `第 ${S.pomo.cycle}/${S.pomo.cycles} 顆 · ` +
      (S.pomo.phase === "focus" ? "專注中" : "休息中") + ` · 剩 ${fmtRemain(pr)}`;
  }
  if (S && S.budget && S.budget.enabled) {
    document.getElementById("budget-info").textContent = budgetInfoText();
  }
}

// 額度剩餘時間用「快照 + 即時外插」顯示：平常（沒有其他倒數在跑）只在每次
// 輪詢（10–30 秒）更新一次，省電；若 tick() 因為別的原因本來就在跑（例如
// 同時鎖定中），就順便即時外插，不需要額外喚醒。
function budgetInfoText() {
  const bg = S.budget;
  if (!bg || !bg.enabled) return "";
  const nowLive = Date.now() / 1000 + clockOffset;
  if (bg.locked_by_budget) {
    const r = bg.window_end - nowLive;
    return r > 0 ? `本視窗額度已用完，${fmtRemain(r)} 後重置` : "額度即將重置…";
  }
  const r = Math.max(0, bg.remaining_seconds - (nowLive - S.now));
  return `本視窗剩餘 ${fmtRemain(r)}（共 ${bg.minutes} 分）`;
}

let budgetFormInit = false;
function populateBudgetForm() {
  const bg = S.budget;
  document.getElementById("budget-toggle").checked = !!(bg && bg.enabled);
  if (bg && bg.enabled) {
    const h = bg.window_hours;
    const useDays = h >= 24 && h % 24 === 0;
    document.getElementById("budget-window-val").value = useDays ? h / 24 : h;
    document.getElementById("budget-window-unit").value = useDays ? "days" : "hours";
    document.getElementById("budget-minutes").value = bg.minutes;
  }
}
function saveBudget() {
  const enabled = document.getElementById("budget-toggle").checked;
  const val = parseFloat(document.getElementById("budget-window-val").value);
  const unit = document.getElementById("budget-window-unit").value;
  const minutes = parseFloat(document.getElementById("budget-minutes").value);
  const window_hours = unit === "days" ? val * 24 : val;
  api("/api/budget/config", {enabled, window_hours, minutes});
}
function lock(min) { api("/api/lock", {minutes: min}); }
function lockCustom() {
  const v = parseInt(document.getElementById("custom-min").value);
  if (v > 0) lock(v);
}
function unlock() {
  if (!S.locked) return;
  armConfirm(document.getElementById("unlock-btn"), "再按一次確認解除", () => {
    api("/api/unlock").then(ok => {
      if (ok) flash("已解除鎖定。已開著的封鎖頁會在 30 秒內自動恢復，或手動重新整理即可", true);
    });
  });
}
function addSite() {
  const el = document.getElementById("new-site");
  if (el.value.trim()) api("/api/sites", {site: el.value}).then(ok => { if (ok) el.value = ""; });
}
function addSchedule() {
  const days = [...picker.querySelectorAll("input:checked")].map(c => +c.value);
  api("/api/schedules", {
    days,
    start: document.getElementById("sch-start").value,
    end: document.getElementById("sch-end").value,
  });
}
function setStrict(on) {
  api("/api/strict", {on}).then(ok => { if (!ok) render(); });
}
function setBlockAll(on) {
  api("/api/block_all", {on}).then(ok => { if (!ok) render(); });
}
function setAutostart(on) {
  api("/api/autostart", {on}).then(ok => { if (!ok) render(); });
}
function startPomo() {
  api("/api/pomo/start", {
    focus: +document.getElementById("pomo-focus").value,
    break: +document.getElementById("pomo-break").value,
    cycles: +document.getElementById("pomo-cycles").value,
  });
}
function stopPomo() {
  armConfirm(document.getElementById("pomo-stop"), "再按一次確認中止",
    () => api("/api/pomo/stop"));
}
// 兩段式確認：再按一次才執行。不用 window.confirm——瀏覽器若被設為
// 「封鎖此網頁的對話方塊」，confirm 會直接回 false，按鈕看起來就像壞掉。
function armConfirm(btn, label, onYes) {
  if (btn.dataset.armed) {
    delete btn.dataset.armed;
    btn.textContent = btn.dataset.orig;
    onYes();
    return;
  }
  btn.dataset.orig = btn.textContent;
  btn.dataset.armed = "1";
  btn.textContent = label;
  setTimeout(() => {
    if (btn.dataset.armed) { delete btn.dataset.armed; btn.textContent = btn.dataset.orig; }
  }, 6000);
}

// 緊急使用用「明確的確認列」而非逾時式確認：真的有急事時，
// 不該因為考慮太久就被自動取消，也不依賴系統對話框。
let emgArming = false;
function armEmergency() { emgArming = true; render(); }
function cancelEmergency() { emgArming = false; render(); }
function doEmergency() {
  emgArming = false;
  api("/api/emergency").then(ok => {
    if (ok) flash(`緊急使用中，${S.emergency.minutes} 分鐘後自動鎖回去`, true);
    else render();
  });
}
function addAllow() {
  const el = document.getElementById("new-allow");
  if (el.value.trim()) api("/api/allow", {site: el.value}).then(ok => { if (ok) el.value = ""; });
}
const BUILD = "__BUILD__";
async function refresh() {
  const r = await fetch("/api/state");
  S = await r.json();
  if (S.build && S.build !== BUILD) { location.reload(); return; }  // 程式已更新 → 自動載入新頁面
  render();
}
refresh();  // render() 會啟動對應節奏的省電輪詢
</script>
</body>
</html>
"""


def already_running():
    """檢查是否已有實例在 8850 服務（Werkzeug 的 SO_REUSEADDR 不會擋重複綁定）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:  # pythonw 無主控台 → 寫到 log 檔
        _log = open(os.path.join(BASE_DIR, "detox.log"), "a", encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = _log
    try:  # 主控台可能是 cp950，避免印中文/符號時當掉
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # 省電：不記每個請求，減少磁碟寫入與 log 膨脹
    if already_running():
        print("Digital Detox 已在執行，直接開啟控制台。")
        webbrowser.open(f"http://localhost:{PORT}")
        sys.exit(0)
    start_block_server(BLOCK_PAGE_PORT)   # 封鎖提示頁（接住 hosts 導向的 http）
    start_block_server(PROXY_PORT)        # 全部封鎖模式的黑洞代理
    threading.Thread(target=enforcer_loop, daemon=True).start()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    print(f"Digital Detox 已啟動 → http://localhost:{PORT}")
    if not is_admin():
        print("⚠️ 未以系統管理員執行，無法真正封鎖網站。請用 start.bat 啟動。")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
