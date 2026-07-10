# -*- coding: utf-8 -*-
"""
Digital Detox — Windows 網站鎖定工具（類 Freedom）
原理：把封鎖清單寫進系統 hosts 檔（導向 0.0.0.0），所有瀏覽器一起生效。
需要以「系統管理員」身分執行（用 start.bat 啟動會自動要求提權）。

py app.py → http://localhost:8850
"""

import ctypes
import json
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
}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_STATE)
        merged.update(data)
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


def lock_status(st):
    """回傳 (locked: bool, until: timestamp|None, source: str)"""
    now_ts = time.time()
    manual = st["lock_until"] if st["lock_until"] > now_ts else None
    sched_end = active_schedule_end(st)
    if sched_end is not None and st["skip_until"] >= sched_end:
        sched_end = None  # 這個時段已被使用者跳過
    if manual and sched_end:
        return True, max(manual, sched_end), "both"
    if manual:
        return True, manual, "manual"
    if sched_end:
        return True, sched_end, "schedule"
    return False, None, "none"


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
<style>
  body {{ background:#10141a; color:#e8edf3; font-family:"Segoe UI","Microsoft JhengHei",sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .box {{ text-align:center; padding:40px 24px; max-width:480px; }}
  .lock {{ font-size:64px; }}
  h1 {{ font-size:24px; margin:16px 0 8px; }}
  .host {{ color:#ff6b6b; font-weight:700; }}
  .remain {{ color:#8b98a8; font-size:14px; margin-bottom:28px; }}
  a.btn {{ display:inline-block; background:#4da3ff; color:#06121f; text-decoration:none;
          font-size:17px; font-weight:700; padding:14px 32px; border-radius:12px; }}
  a.small {{ display:block; margin-top:24px; color:#8b98a8; font-size:12px; text-decoration:none; }}
</style></head>
<body><div class="box">
  <div class="lock">🔒</div>
  <h1><span class="host">{host}</span> 已被封鎖</h1>
  <div class="remain">{remain or "專心時間進行中"}</div>
  <a class="btn" href="{HEPTABASE_URL}">📝 前往 Heptabase 寫筆記</a>
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
        self.send_response(403)
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, *args):
        pass


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


def build_pac(allow_sites):
    rules = "".join(
        f'  if (host === "{s}" || shExpMatch(host, "*.{s}")) return "DIRECT";\n'
        for s in allow_sites
    )
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


def apply_pac(on):
    """設定/移除系統代理自動設定（HKCU，不需管理員）。冪等。"""
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
        if on and current != PAC_URL:
            winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, PAC_URL)
            _refresh_wininet()
        elif not on and current == PAC_URL:
            winreg.DeleteValue(key, "AutoConfigURL")
            _refresh_wininet()
    finally:
        winreg.CloseKey(key)


def enforcer_loop():
    """背景每 15 秒檢查一次：該鎖就鎖、該解就解。"""
    while True:
        with state_lock:
            locked, _, _ = lock_status(state)
            apply_hosts(locked, state["sites"])
            apply_pac(locked and state["block_all"])
        time.sleep(15)


# ---------- API ----------

def state_payload():
    locked, until, source = lock_status(state)
    return {
        "locked": locked,
        "until": until,
        "source": source,
        "strict": state["strict"],
        "sites": state["sites"],
        "schedules": state["schedules"],
        "block_all": state["block_all"],
        "allow_sites": state["allow_sites"],
        "admin": is_admin(),
        "hosts_error": hosts_error,
        "block_page_error": block_page_error,
        "now": time.time(),
    }


def enforce_now():
    """依目前狀態立刻套用 hosts 與系統代理。呼叫前需持有 state_lock。"""
    locked, _, _ = lock_status(state)
    apply_hosts(locked, state["sites"])
    apply_pac(locked and state["block_all"])


@app.get("/api/state")
def api_state():
    with state_lock:
        return jsonify(state_payload())


@app.post("/api/lock")
def api_lock():
    minutes = int(request.json.get("minutes", 0))
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
        sched_end = active_schedule_end(state)
        if sched_end:
            state["skip_until"] = sched_end  # 跳過目前這個排程時段
        save_state(state)
        enforce_now()
        return jsonify(state_payload())


@app.post("/api/sites")
def api_add_site():
    site = normalize_site(request.json.get("site", ""))
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
    site = request.json.get("site", "")
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
    data = request.json
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
    sid = int(request.json.get("id", -1))
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
    on = bool(request.json.get("on"))
    with state_lock:
        locked, _, _ = lock_status(state)
        if not on and locked and state["strict"]:
            return jsonify({"error": "鎖定期間無法關閉嚴格模式"}), 403
        state["strict"] = on
        save_state(state)
        return jsonify(state_payload())


@app.post("/api/block_all")
def api_block_all():
    on = bool(request.json.get("on"))
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
    site = normalize_site(request.json.get("site", ""))
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
    site = request.json.get("site", "")
    with state_lock:
        if site in state["allow_sites"]:
            state["allow_sites"].remove(site)
            save_state(state)
        return jsonify(state_payload())


@app.get("/proxy.pac")
def proxy_pac():
    with state_lock:
        pac = build_pac(state["allow_sites"])
    return pac, 200, {"Content-Type": "application/x-ns-proxy-autoconfig"}


@app.get("/blocked")
def blocked_preview():
    return render_block_page(request.args.get("site", "example.com"))


@app.get("/")
def index():
    return PAGE


# ---------- 前端頁面 ----------

PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Digital Detox</title>
<style>
  :root {
    --bg: #10141a; --card: #1a212b; --line: #2a3442;
    --text: #e8edf3; --dim: #8b98a8; --accent: #4da3ff;
    --ok: #3ecf8e; --bad: #ff6b6b; --warn: #ffb84d;
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", "Microsoft JhengHei", sans-serif;
    max-width: 680px; margin: 0 auto; padding: 24px 16px 60px;
  }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 18px; margin-bottom: 16px;
  }
  .card h2 { font-size: 15px; color: var(--dim); font-weight: 600; margin-bottom: 12px; }
  #status-big { font-size: 28px; font-weight: 700; }
  #status-big.locked { color: var(--bad); }
  #status-big.free { color: var(--ok); }
  #countdown { font-size: 15px; color: var(--dim); margin-top: 4px; }
  .banner {
    border-radius: 10px; padding: 10px 14px; font-size: 13px;
    margin-bottom: 16px; display: none;
  }
  .banner.show { display: block; }
  .banner.warn { background: #3a2d14; color: var(--warn); border: 1px solid #5a4620; }
  button {
    background: var(--accent); color: #06121f; border: 0; border-radius: 8px;
    padding: 9px 16px; font-size: 14px; font-weight: 600; cursor: pointer;
    font-family: inherit;
  }
  button.ghost { background: transparent; color: var(--dim); border: 1px solid var(--line); }
  button.danger { background: var(--bad); color: #fff; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input[type=text], input[type=number], input[type=time] {
    background: #0d1117; border: 1px solid var(--line); color: var(--text);
    border-radius: 8px; padding: 9px 12px; font-size: 14px; font-family: inherit;
  }
  input[type=number] { width: 80px; }
  ul { list-style: none; margin-top: 10px; }
  li {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 4px; border-bottom: 1px solid var(--line); font-size: 14px;
  }
  li:last-child { border-bottom: 0; }
  li button { padding: 4px 10px; font-size: 12px; }
  .days label {
    display: inline-block; padding: 5px 9px; border: 1px solid var(--line);
    border-radius: 7px; font-size: 13px; cursor: pointer; user-select: none;
  }
  .days input { display: none; }
  .days input:checked + span { color: var(--accent); font-weight: 700; }
  .switch { display: flex; align-items: center; gap: 10px; font-size: 14px; }
  #msg { color: var(--bad); font-size: 13px; min-height: 18px; margin-top: 8px; }
</style>
</head>
<body>
  <h1>🔒 Digital Detox</h1>
  <div class="sub">封鎖分心網站 · 對所有瀏覽器生效（hosts 層級）</div>

  <div id="admin-banner" class="banner warn">
    ⚠️ 目前不是以系統管理員執行，無法寫入 hosts 檔 — 請關閉後用 <b>start.bat</b> 啟動。
  </div>
  <div id="hosts-banner" class="banner warn"></div>
  <div id="blockpage-banner" class="banner warn"></div>

  <div class="card">
    <div id="status-big" class="free">—</div>
    <div id="countdown"></div>
  </div>

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
      🌐 <b>全部封鎖</b>：鎖定時封鎖所有網站，只有下方白名單可以連
    </label>
  </div>

  <div class="card">
    <h2>白名單（全部封鎖時仍可連線，含子網域）</h2>
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
      鎖定期間<b>不能</b>提前解除、不能移除網站或刪除排程
    </label>
  </div>

  <div class="sub" style="margin-top:-4px">
    被封鎖的網站會顯示提示頁並可一鍵前往 Heptabase（<a href="/blocked?site=youtube.com"
    target="_blank" style="color:var(--accent)">預覽提示頁</a>）
  </div>

  <div id="msg"></div>

<script>
const DAY_NAMES = ["一","二","三","四","五","六","日"];
let S = null;

const picker = document.getElementById("day-picker");
DAY_NAMES.forEach((n, i) => {
  picker.insertAdjacentHTML("beforeend",
    `<label><input type="checkbox" value="${i}"><span>週${n}</span></label>`);
});

async function api(path, body) {
  const opt = body === undefined ? {} :
    { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json();
  document.getElementById("msg").textContent = r.ok ? "" : (data.error || "發生錯誤");
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
  const big = document.getElementById("status-big");
  if (S.locked) {
    big.textContent = "🚫 鎖定中";
    big.className = "locked";
  } else {
    big.textContent = "✅ 未鎖定";
    big.className = "free";
  }
  tick();
  document.getElementById("admin-banner").classList.toggle("show", !S.admin);
  const hb = document.getElementById("hosts-banner");
  hb.textContent = S.hosts_error ? "⚠️ " + S.hosts_error : "";
  hb.classList.toggle("show", !!S.hosts_error);
  const bb = document.getElementById("blockpage-banner");
  bb.textContent = S.block_page_error ? "⚠️ " + S.block_page_error : "";
  bb.classList.toggle("show", !!S.block_page_error);
  document.getElementById("unlock-btn").disabled = !S.locked || (S.locked && S.strict);
  document.getElementById("strict-toggle").checked = S.strict;
  document.getElementById("blockall-toggle").checked = S.block_all;

  const allowUl = document.getElementById("allow-list");
  allowUl.innerHTML = "";
  S.allow_sites.forEach(site => {
    const li = document.createElement("li");
    li.innerHTML = `<span>✅ ${site}</span>`;
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
}

function tick() {
  const el = document.getElementById("countdown");
  if (S && S.locked && S.until) {
    const remain = S.until - (Date.now() / 1000 + clockOffset);
    if (remain <= 0) { refresh(); return; }
    const src = S.source === "schedule" ? "（排程）" : S.source === "both" ? "（手動＋排程）" : "";
    el.textContent = "剩餘 " + fmtRemain(remain) + " " + src;
  } else {
    el.textContent = S && S.schedules.length ? "等待下一個排程時段" : "";
  }
}
setInterval(tick, 1000);

function lock(min) { api("/api/lock", {minutes: min}); }
function lockCustom() {
  const v = parseInt(document.getElementById("custom-min").value);
  if (v > 0) lock(v);
}
function unlock() {
  if (S.locked && confirm("確定要提前解除鎖定？")) api("/api/unlock");
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
function addAllow() {
  const el = document.getElementById("new-allow");
  if (el.value.trim()) api("/api/allow", {site: el.value}).then(ok => { if (ok) el.value = ""; });
}
async function refresh() {
  const r = await fetch("/api/state");
  S = await r.json();
  render();
}
refresh();
setInterval(refresh, 10000);
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
    app.run(host="127.0.0.1", port=PORT)
