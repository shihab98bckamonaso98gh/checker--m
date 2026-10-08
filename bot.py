"""
Facebook Number Checker — Termux / Mobile / HTTP-Only
=====================================================
Auto-detects numbers.txt, runs immediately.
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
import glob
from collections import deque
from pathlib import Path

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
    from rich.table import Table
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


# ===================== CONFIG =====================
NUMBERS_FILENAME = "numbers.txt"

FILE_EXISTS   = "ACCOUNT_EXISTS.txt"
FILE_DISABLED = "ACCOUNT_DISABLED.txt"
FILE_MULTI    = "MULTIPLE_ACCOUNTS.txt"
FILE_NO       = "NO_ACCOUNT.txt"

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows Mobile 10; Android 10.0; Microsoft; Lumia 950XL) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 "
    "Edge/40.15254.603 VirusTotalBot",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-A515F) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36",
]

BASE_URL = "https://m.facebook.com"
IDENTIFY_URL = f"{BASE_URL}/login/identify/"

MAX_ATTEMPTS    = 4          # more retries for accuracy
REQUEST_TIMEOUT = 15
PAUSE_BETWEEN   = 0.10
# ==================================================

console = Console() if HAVE_RICH else None


# ===================== AUTO-DETECT NUMBERS.TXT =====================

def find_numbers_file():
    """
    Search for numbers.txt in common locations:
    1. Current working directory
    2. Script directory
    3. Termux home directory
    4. /sdcard (if accessible)
    """
    candidates = []

    # 1. Current working directory
    candidates.append(Path.cwd() / NUMBERS_FILENAME)

    # 2. Script's own directory
    try:
        candidates.append(Path(__file__).resolve().parent / NUMBERS_FILENAME)
    except Exception:
        pass

    # 3. Termux home directory
    home = Path.home()
    candidates.append(home / NUMBERS_FILENAME)

    # 4. Common Termux storage paths
    candidates.append(Path("/sdcard") / NUMBERS_FILENAME)
    candidates.append(Path("/storage/emulated/0") / NUMBERS_FILENAME)

    # 5. Any .txt file matching *numbers* in cwd
    for f in glob.glob("*.txt"):
        if "number" in f.lower() and f != NUMBERS_FILENAME:
            candidates.append(Path.cwd() / f)

    # Return the first that exists
    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except Exception:
            continue

    return None


def create_sample(path):
    """Create a sample numbers.txt."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("+1234567890\n+1987654321\n")
    print(f"[+] Sample created: {path}")
    print("    Add your numbers and re-run.")


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


def read_numbers(path):
    """Read numbers from the given path."""
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for n in lines:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


# ===================== HTML PARSING =====================

def extract_form_data(html: str):
    """Extract the identify form action URL and hidden inputs."""
    soup = BeautifulSoup(html, "html.parser")

    form = soup.find("form", id="identify_yourself_flow")
    if not form:
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
    """
    Multi-layer classification for maximum accuracy.
    Returns: ACCOUNT_DISABLED / MULTIPLE_ACCOUNTS / NO_ACCOUNT /
             ACCOUNT_EXISTS / ERROR
    """
    t = html.lower()
    soup = BeautifulSoup(html, "html.parser")

    # ===== LAYER 1: DISABLED =====
    if "account has been disabled" in t or "your account has been disabled" in t:
        return "ACCOUNT_DISABLED"
    for el in soup.find_all(attrs={"data-sigil": "marea"}):
        if "disabled" in (el.get_text() or "").lower():
            return "ACCOUNT_DISABLED"
    # Disabled page often has "Try Again" + "Cancel" buttons
    if 'id="u_0_0_' in html and "try again" in t and "disabled" in t:
        return "ACCOUNT_DISABLED"

    # ===== LAYER 2: MULTIPLE ACCOUNTS =====
    # Text heading
    if "choose your account" in t:
        return "MULTIPLE_ACCOUNTS"
    # Structural: form#login_form with identifier= in action
    login_form = soup.find("form", id="login_form")
    if login_form:
        action = login_form.get("action", "") or ""
        if "identifier=" in action:
            return "MULTIPLE_ACCOUNTS"
    # Multiple <a class="touchable primary"> inside data-sigil="marea"
    account_links = 0
    for area in soup.find_all(attrs={"data-sigil": "marea"}):
        for a in area.find_all("a", class_="touchable"):
            account_links += 1
    if account_links >= 2:
        return "MULTIPLE_ACCOUNTS"
    # Multiple profile image placeholders
    if html.count('class="img img _1-yc _2sxw"') >= 2:
        return "MULTIPLE_ACCOUNTS"

    # ===== LAYER 3: NO ACCOUNT =====
    if "login_identify_search_error_msg" in html:
        return "NO_ACCOUNT"
    if "doesn't match an account" in t or "does not match an account" in t:
        return "NO_ACCOUNT"
    if "no account found" in t:
        return "NO_ACCOUNT"
    # Error div by data-sigil
    for el in soup.find_all(attrs={"data-sigil": "marea"}):
        txt = (el.get_text() or "").lower()
        if "doesn't match" in txt or "no account" in txt:
            return "NO_ACCOUNT"

    # ===== LAYER 4: ACCOUNT EXISTS =====
    if 'type="password"' in t:
        return "ACCOUNT_EXISTS"
    if "try entering your password" in t:
        return "ACCOUNT_EXISTS"
    if "send login code" in t or "send code" in t:
        return "ACCOUNT_EXISTS"
    if "enter the code" in t:
        return "ACCOUNT_EXISTS"
    # Password input element
    if soup.find("input", {"type": "password"}):
        return "ACCOUNT_EXISTS"
    # Password form with data-testid
    if soup.find("input", attrs={"data-testid": "conf_password_input"}):
        return "ACCOUNT_EXISTS"

    # ===== FALLBACK =====
    # If identify form is gone -> forwarded
    if 'id="identify_search_text_input"' not in html and 'name="email"' not in html:
        return "ACCOUNT_EXISTS"
    # Still on form, no error element -> assume exists (user rule)
    return "ACCOUNT_EXISTS"


# ===================== CORE HTTP CHECK =====================

async def check_number(client: httpx.AsyncClient, number: str) -> str:
    """
    Full HTTP flow for one number.
    Uses a fresh cookie jar each time for consistency.
    """
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
        "Connection": "keep-alive",
    }

    # --- Step 1: GET identify page (fresh session) ---
    r = await client.get(IDENTIFY_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    action_url, form_data = extract_form_data(r.text)
    if not action_url:
        # Retry once with a short delay
        await asyncio.sleep(0.5)
        r = await client.get(IDENTIFY_URL, headers=headers,
                             timeout=REQUEST_TIMEOUT)
        action_url, form_data = extract_form_data(r.text)
        if not action_url:
            return "ERROR"

    # --- Step 2: POST the number ---
    post_data = dict(form_data)
    post_data["email"] = number
    post_data.setdefault("did_submit", "Search")

    r2 = await client.post(action_url, data=post_data,
                           headers=headers, timeout=REQUEST_TIMEOUT)
    r2.raise_for_status()

    # --- Step 3: Classify ---
    result = classify_response(r2.text)

    # --- Step 4: If ambiguous, retry with a FRESH client ---
    if result == "ERROR":
        return "ERROR"

    return result


async def check_number_with_retry(number: str) -> str:
    """
    Wraps check_number with a fresh httpx client each attempt.
    This handles cookie/rate-limit inconsistencies.
    """
    last_result = "ERROR"
    for attempt in range(MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=REQUEST_TIMEOUT,
                limits=httpx.Limits(max_connections=4,
                                    max_keepalive_connections=2),
            ) as client:
                result = await check_number(client, number)
                if result != "ERROR":
                    return result
                last_result = result
        except Exception:
            pass
        # Exponential backoff between attempts
        await asyncio.sleep(0.5 * (attempt + 1) + random.uniform(0, 0.3))

    return last_result


# ===================== LIVE UI (RESPONSIVE) =====================

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
                             refresh_per_second=4, transient=False,
                             vertical_overflow="visible")

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
        """Build a responsive footer that adapts to any terminal width."""
        try:
            term_w = console.size.width
        except Exception:
            term_w = 80

        # ---- Responsive bar width ----
        # On narrow mobile screens use shorter bar
        if term_w < 50:
            bar_w = max(8, term_w - 30)
        else:
            bar_w = max(15, min(term_w - 55, 45))

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

        # ---- Build with responsive layout ----
        line1 = Text()
        line1.append("[", style="grey50")
        line1.append(bar, style="bold green")
        line1.append("]", style="grey50")
        line1.append(f" {self.done}/{self.total}", style="bold white")
        line1.append(f" {pct*100:.0f}%", style="bold cyan")
        line1.append(f" {rate:.1f}/s", style="magenta")
        if term_w >= 50:
            line1.append(f" ETA {eta}", style="yellow")

        # ---- Category line (stacks on narrow screens) ----
        if term_w >= 60:
            # Wide: single line
            line2 = Text()
            line2.append("EXISTS ", style="grey50")
            line2.append(f"{self.stats['ACCOUNT_EXISTS']}", style="bold green")
            line2.append("  DISABLED ", style="grey50")
            line2.append(f"{self.stats['ACCOUNT_DISABLED']}", style="bold yellow")
            line2.append("  MULTI ", style="grey50")
            line2.append(f"{self.stats['MULTIPLE_ACCOUNTS']}", style="bold cyan")
            line2.append("  NO_ACC ", style="grey50")
            line2.append(f"{self.stats['NO_ACCOUNT']}", style="bold red")
        else:
            # Narrow: two lines
            line2 = Text()
            line2.append("E:", style="grey50")
            line2.append(f"{self.stats['ACCOUNT_EXISTS']}", style="bold green")
            line2.append(" D:", style="grey50")
            line2.append(f"{self.stats['ACCOUNT_DISABLED']}", style="bold yellow")
            line2.append(" M:", style="grey50")
            line2.append(f"{self.stats['MULTIPLE_ACCOUNTS']}", style="bold cyan")
            line2.append(" N:", style="grey50")
            line2.append(f"{self.stats['NO_ACCOUNT']}", style="bold red")

        content = Text.assemble(line1, "\n", line2)
        return Panel(
            content,
            title="[bold white]⚡ Live Progress[/bold white]",
            subtitle=f"[grey50]{self.workers}W[/grey50]",
            border_style="bright_blue",
            padding=(0, 1),
        )

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

        # Use fresh client per check for consistency
        try:
            status = await check_number_with_retry(number)
        except Exception:
            status = "ERROR"

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

        await asyncio.sleep(PAUSE_BETWEEN + random.uniform(0, 0.1))


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
    print("=" * 60)
    print("  Facebook Number Checker — Termux / HTTP mode")
    print("=" * 60)

    # ---- Auto-detect numbers.txt ----
    numbers_path = find_numbers_file()

    if numbers_path is None:
        # Not found anywhere -> create in cwd
        numbers_path = os.path.abspath(NUMBERS_FILENAME)
        create_sample(numbers_path)
        return

    print(f"[+] Auto-detected: {numbers_path}")

    numbers = read_numbers(numbers_path)
    if not numbers:
        print("[!] File is empty. Add numbers and re-run.")
        return

    workers = detect_workers()
    print(f"[+] Loaded {len(numbers)} unique numbers")
    print(f"[+] Workers: {workers}")
    print()

    try:
        asyncio.run(main_async(numbers_path, numbers, workers))
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")


if __name__ == "__main__":
    main()
