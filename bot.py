"""
Facebook Number Checker — Termux / Mobile / HTTP-Only
=====================================================
On startup it asks for the numbers.txt path, then runs immediately.
No browser. No Playwright. Pure HTTP via httpx.

Outputs 4 files next to numbers.txt:
    ACCOUNT_EXISTS.txt
    ACCOUNT_DISABLED.txt
    MULTIPLE_ACCOUNTS.txt
    NO_ACCOUNT.txt

Educational use only.
"""

import asyncio
import os
import re
import sys
import time
import random
from collections import deque

# ---------- hard dependencies ----------
try:
    import httpx
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"[!] Missing dependency: {e.name}")
    print("    Install with:  pip install httpx beautifulsoup4")
    sys.exit(1)

# ---------- optional rich (falls back to plain output) ----------
try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


# ===================== CONFIG =====================
DEFAULT_NUMBERS_PATH = "numbers.txt"

FILE_EXISTS   = "ACCOUNT_EXISTS.txt"
FILE_DISABLED = "ACCOUNT_DISABLED.txt"
FILE_MULTI    = "MULTIPLE_ACCOUNTS.txt"
FILE_NO       = "NO_ACCOUNT.txt"

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows Mobile 10; Android 10.0; Microsoft; Lumia 950XL) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 "
    "Edge/40.15254.603 VirusTotalBot",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1",
]

BASE_URL = "https://m.facebook.com"
IDENTIFY_URL = f"{BASE_URL}/login/identify/"

MAX_ATTEMPTS    = 3
REQUEST_TIMEOUT = 15
PAUSE_BETWEEN   = 0.15
# ==================================================

console = Console() if HAVE_RICH else None


# ===================== TERMUX / DEVICE DETECTION =====================

def detect_workers():
    """
    Decide sensible default worker count based on environment.
    Override with the FB_WORKERS env variable.
    """
    env = os.environ.get("FB_WORKERS")
    if env and env.isdigit():
        n = int(env)
        return max(1, min(n, 32))

    if "com.termux" in os.environ.get("PREFIX", ""):
        return 4                                  # Termux / Android
    if sys.platform.startswith("win"):
        return 10                                 # Windows PC
    if sys.platform == "darwin":
        return 8                                  # macOS
    return 6                                      # generic Linux


# ===================== PATH PROMPT =====================

def ask_numbers_path():
    """
    Ask the user for the path to numbers.txt.
    Enter = default ./numbers.txt
    Also accepts drag-and-drop paths (quotes stripped).
    """
    print("=" * 60)
    print("  Facebook Number Checker — Termux / HTTP mode")
    print("=" * 60)
    default_abs = os.path.abspath(DEFAULT_NUMBERS_PATH)
    try:
        raw = input(f"Enter path to numbers.txt\n"
                    f"(press Enter for: {default_abs})\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raw = ""

    if raw == "":
        return default_abs

    # strip surrounding quotes (drag-and-drop on PC adds them)
    raw = raw.strip().strip("'").strip('"')

    # expand ~ and env vars
    raw = os.path.expanduser(os.path.expandvars(raw))

    if not os.path.isabs(raw):
        raw = os.path.abspath(raw)

    return raw


def read_numbers(path):
    """Read numbers from the given path. Create a sample if missing."""
    if not os.path.exists(path):
        print(f"[!] File not found: {path}")
        try:
            ans = input("    Create a sample file? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans == "y":
            with open(path, "w", encoding="utf-8") as f:
                f.write("+1234567890\n+1987654321\n")
            print(f"[+] Sample created at {path}. Add numbers and re-run.")
        return []

    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


# ===================== HTML PARSING =====================

def extract_form_data(html: str):
    """Extract the identify form action URL and hidden inputs."""
    soup = BeautifulSoup(html, "html.parser")

    form = soup.find("form", id="identify_yourself_flow")
    if not form:
        # Fallback: any form containing an input named 'email'
        for f in soup.find_all("form"):
            if f.find("input", {"name": "email"}):
                form = f
                break

    if not form:
        return None, None

    action = (form.get("action") or "").strip()
    if action.startswith("/"):
        action = BASE_URL + action
    elif not action.startswith("http"):
        action = IDENTIFY_URL

    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            data[name] = inp.get("value", "")

    return action, data


def classify_response(html: str) -> str:
    """Return one of: ACCOUNT_DISABLED / MULTIPLE_ACCOUNTS / NO_ACCOUNT / ACCOUNT_EXISTS / ERROR."""
    t = html.lower()

    # 1. Disabled
    if "account has been disabled" in t or "your account has been disabled" in t:
        return "ACCOUNT_DISABLED"

    # 2. Multiple accounts
    if "choose your account" in t:
        return "MULTIPLE_ACCOUNTS"

    # 3. No account
    if "login_identify_search_error_msg" in t:
        return "NO_ACCOUNT"
    if "doesn't match an account" in t or "does not match an account" in t:
        return "NO_ACCOUNT"
    if "no account found" in t:
        return "NO_ACCOUNT"

    # 4. Exists — forwarded page
    if 'type="password"' in t:
        return "ACCOUNT_EXISTS"
    if "try entering your password" in t:
        return "ACCOUNT_EXISTS"
    if "send login code" in t or "send code" in t:
        return "ACCOUNT_EXISTS"
    if "enter the code" in t:
        return "ACCOUNT_EXISTS"

    # Identify form gone => forwarded
    if 'id="identify_search_text_input"' not in t and 'name="email"' not in t:
        return "ACCOUNT_EXISTS"

    # Fallback: no error element present => assume exists
    return "ACCOUNT_EXISTS"


# ===================== CORE HTTP CHECK =====================

async def check_number(client: httpx.AsyncClient, number: str) -> str:
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }

    # GET identify page
    r = await client.get(IDENTIFY_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    action_url, form_data = extract_form_data(r.text)
    if not action_url:
        await asyncio.sleep(0.5)
        r = await client.get(IDENTIFY_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        action_url, form_data = extract_form_data(r.text)
        if not action_url:
            return "ERROR"

    # POST with the number
    post_data = dict(form_data)
    post_data["email"] = number
    post_data.setdefault("did_submit", "Search")

    r2 = await client.post(action_url, data=post_data,
                           headers=headers, timeout=REQUEST_TIMEOUT)
    r2.raise_for_status()

    return classify_response(r2.text)


# ===================== LIVE UI =====================

class LiveUI:
    def __init__(self, total, workers):
        self.total = total
        self.workers = workers
        self.stats = {
            "ACCOUNT_EXISTS": 0,
            "ACCOUNT_DISABLED": 0,
            "MULTIPLE_ACCOUNTS": 0,
            "NO_ACCOUNT": 0,
        }
        self.done = 0
        self.t_start = time.time()
        self.lock = asyncio.Lock()
        self.live = None

        if HAVE_RICH:
            self.live = Live(self._build(), console=console,
                             refresh_per_second=4, transient=False)

    def start(self):
        if self.live:
            self.live.start()

    def stop(self):
        if self.live:
            try:
                self.live.stop()
            except Exception:
                pass

    def _build(self):
        try:
            term_w = console.size.width
        except Exception:
            term_w = 80
        bar_w = max(15, min(term_w - 50, 40))
        pct = self.done / max(self.total, 1)
        filled = int(bar_w * pct)
        bar = "█" * filled + "░" * (bar_w - filled)
        elapsed = time.time() - self.t_start
        rate = self.done / max(elapsed, 0.01)

        if rate > 0 and self.done < self.total:
            eta_s = (self.total - self.done) / rate
            eta = f"{int(eta_s // 60):02d}:{int(eta_s % 60):02d}"
        else:
            eta = "--:--"

        line1 = Text()
        line1.append("[", style="grey50")
        line1.append(bar, style="bold green")
        line1.append("] ", style="grey50")
        line1.append(f"{self.done:>4}/{self.total:<4}", style="bold white")
        line1.append(f"  {pct*100:5.1f}%", style="bold cyan")
        line1.append(f"   {rate:5.2f}/s", style="magenta")
        line1.append(f"   ETA {eta}", style="yellow")

        line2 = Text()
        line2.append("  EXISTS ", style="grey50")
        line2.append(f"{self.stats['ACCOUNT_EXISTS']:>5}", style="bold green")
        line2.append("   DISABLED ", style="grey50")
        line2.append(f"{self.stats['ACCOUNT_DISABLED']:>5}", style="bold yellow")
        line2.append("   MULTIPLE ", style="grey50")
        line2.append(f"{self.stats['MULTIPLE_ACCOUNTS']:>5}", style="bold cyan")
        line2.append("   NO_ACCOUNT ", style="grey50")
        line2.append(f"{self.stats['NO_ACCOUNT']:>5}", style="bold red")

        content = Text.assemble(line1, "\n", line2)
        return Panel(content,
                     title="[bold white]⚡ Live Progress[/bold white]",
                     subtitle=f"[grey50]{self.workers} workers[/grey50]",
                     border_style="bright_blue",
                     padding=(0, 1))

    async def log(self, msg):
        async with self.lock:
            if self.live:
                console.print(msg, highlight=False, markup=False)
            else:
                print(msg, flush=True)

    async def record(self, status):
        async with self.lock:
            self.stats[status] = self.stats.get(status, 0) + 1
            self.done += 1
            if self.live:
                self.live.update(self._build(), refresh=True)


# ===================== WORKER =====================

async def worker(wid, queue, queue_lock, records, records_lock,
                 pending, pending_lock, ui, total, t_start):
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    ) as client:
        while True:
            async with pending_lock:
                if pending[0] <= 0:
                    return

            async with queue_lock:
                if queue:
                    number, attempt = queue.popleft()
                else:
                    number = None

            if number is None:
                await asyncio.sleep(0.05)
                continue

            try:
                status = await check_number(client, number)
            except Exception:
                status = "ERROR"

            # retry silently
            if status == "ERROR" and attempt < MAX_ATTEMPTS:
                async with queue_lock:
                    queue.appendleft((number, attempt + 1))
                await asyncio.sleep(0.5 * attempt)
                continue

            if status == "ERROR":
                status = "NO_ACCOUNT"   # fallback

            async with records_lock:
                records.append((number, status))
            async with pending_lock:
                pending[0] -= 1

            done = total - pending[0]
            rate = done / max(time.time() - t_start, 0.01)
            await ui.log(f"[{done:>4}/{total}] W{wid} "
                         f"{number:<17} → {status:<18} ({rate:5.2f}/s)")
            await ui.record(status)

            await asyncio.sleep(PAUSE_BETWEEN)


# ===================== MAIN =====================

async def main_async(numbers_path, numbers, workers):
    total = len(numbers)
    ui = LiveUI(total=total, workers=workers)
    ui.start()

    await ui.log(f"[setup] numbers : {numbers_path}")
    await ui.log(f"[setup] {total} numbers | {workers} workers | HTTP mode")

    records = []
    records_lock = asyncio.Lock()
    queue = deque((n, 1) for n in numbers)
    queue_lock = asyncio.Lock()
    pending = [total]
    pending_lock = asyncio.Lock()
    t_start = time.time()

    try:
        tasks = [
            asyncio.create_task(
                worker(i + 1, queue, queue_lock, records, records_lock,
                       pending, pending_lock, ui, total, t_start)
            )
            for i in range(workers)
        ]
        await asyncio.gather(*tasks)
    finally:
        ui.stop()

    elapsed = time.time() - t_start

    # ---- Group ----
    groups = {
        "ACCOUNT_EXISTS": [],
        "ACCOUNT_DISABLED": [],
        "MULTIPLE_ACCOUNTS": [],
        "NO_ACCOUNT": [],
    }
    for n, s in records:
        if s in groups:
            groups[s].append(n)
        else:
            groups["NO_ACCOUNT"].append(n)

    # ---- Write 4 files next to numbers.txt ----
    out_dir = os.path.dirname(numbers_path) or "."
    written = {}
    for key, fname in (
        ("ACCOUNT_EXISTS", FILE_EXISTS),
        ("ACCOUNT_DISABLED", FILE_DISABLED),
        ("MULTIPLE_ACCOUNTS", FILE_MULTI),
        ("NO_ACCOUNT", FILE_NO),
    ):
        full = os.path.join(out_dir, fname)
        with open(full, "w", encoding="utf-8") as f:
            f.write("\n".join(groups[key]))
            if groups[key]:
                f.write("\n")
        written[key] = (full, len(groups[key]))

    # ---- Summary ----
    yes = len(groups["ACCOUNT_EXISTS"])
    dis = len(groups["ACCOUNT_DISABLED"])
    mul = len(groups["MULTIPLE_ACCOUNTS"])
    no  = len(groups["NO_ACCOUNT"])

    print()
    print("=" * 60)
    print("  DONE")
    print("=" * 60)
    print(f"  Total              : {total}")
    print(f"  Has account        : {yes}")
    print(f"  Disabled account   : {dis}")
    print(f"  Multiple accounts  : {mul}")
    print(f"  No account         : {no}")
    print(f"  Elapsed            : {elapsed:.1f}s")
    print(f"  Throughput         : {total/max(elapsed,0.01):.2f} numbers/sec")
    print("-" * 60)
    for key, (path, count) in written.items():
        print(f"  {os.path.basename(path):<24} ({count} numbers)")
    print("=" * 60)


def main():
    numbers_path = ask_numbers_path()

    if not os.path.exists(numbers_path):
        print(f"[!] Path does not exist: {numbers_path}")
        return

    numbers = read_numbers(numbers_path)
    if not numbers:
        return

    workers = detect_workers()
    env_note = " (override with FB_WORKERS)" if "FB_WORKERS" not in os.environ else ""
    print(f"[+] Loaded {len(numbers)} numbers from: {numbers_path}")
    print(f"[+] Workers: {workers}{env_note}")
    print()

    try:
        asyncio.run(main_async(numbers_path, numbers, workers))
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")


if __name__ == "__main__":
    main()