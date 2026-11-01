#!/usr/bin/env python3
"""
HMT Watches stock alert (men's page by default).
Renders hmtwatches.in listing pages in a headless browser, clicks "Load More" until
every product is loaded, tracks each product's status (coming_soon / out_of_stock /
in_stock) and notifies you when a product that was NOT in stock becomes in stock.

Usage:
  python monitor.py --dump     # show what it detects, no state saved, no alerts
  python monitor.py            # normal run (first run only records a baseline)
  python monitor.py --test     # send a test notification

Config via environment variables:
  NTFY_TOPIC      ntfy.sh topic -> push to phone
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, MAIL_TO   -> email (optional)
  HMT_PAGES       comma list of pages, default: mens
  HMT_EXCLUDE     skip products whose name contains these words, default: women,ladies
  HMT_WATCH       optional keywords; only alert for names containing one (e.g. "gandaberunda,vijay")
  NOTIFY_NEW      "1" to also alert on brand-new listings that appear already in stock
  STATE_FILE      default state.json
"""
import argparse, json, os, re, smtplib, sys
from email.message import EmailMessage
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "https://www.hmtwatches.in"
PAGES = [p.strip() for p in os.getenv("HMT_PAGES", "mens").split(",") if p.strip()]
WATCH = [w.strip().lower() for w in os.getenv("HMT_WATCH", "").split(",") if w.strip()]
EXCLUDE = [w.strip().lower() for w in os.getenv("HMT_EXCLUDE", "women,ladies").split(",") if w.strip()]
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
NOTIFY_NEW = os.getenv("NOTIFY_NEW") == "1"
PRODUCT_LINK = "product_overview"   # product URLs look like /product_overview?id=<encrypted>

# The ?id= part of product URLs is encrypted and changes on every page load, so the
# product's image path is used as its stable identity instead.
EXTRACT_JS = r"""
() => {
  const anchors = [...document.querySelectorAll('a[href*="product_overview"]')].filter(a => a.querySelector('img'));
  const hrefs = new Set(anchors.map(a => a.href));
  const out = [];
  const seen = new Set();
  for (const a of anchors) {
    const img = a.querySelector('img');
    let src = [img.getAttribute('data-src'), img.currentSrc, img.src].find(s => s && !s.startsWith('data:')) || '';
    src = src.split('?')[0];
    let card = a;
    while (card.parentElement) {
      const p = card.parentElement;
      const n = new Set([...p.querySelectorAll('a[href*="product_overview"]')].map(x => x.href).filter(h => hrefs.has(h)));
      if (n.size > 1) break;
      card = p;
    }
    const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
    const sig = src + '|' + text;
    if (seen.has(sig) && src) continue;     // same card seen through a second link
    seen.add(sig);
    out.push({ url: a.href, img: src, text, title: (a.title || img.alt || '').trim() });
  }
  return out;
}
"""

def classify(text: str) -> str:
    t = text.lower()
    if "coming soon" in t or "notify me" in t or "launching soon" in t:
        return "coming_soon"
    if "out of stock" in t or "sold out" in t:
        return "out_of_stock"
    if "add to cart" in t or "buy now" in t:
        return "in_stock"
    # Cards for purchasable watches show only name + price (cart is an icon, no text).
    if re.search(r"(rs\.?|₹)\s*[\d,]+", t):
        return "in_stock"
    return "unknown"

def name_of(it):
    t = it["text"]
    m = re.split(r"coming soon|out of stock|sold out|\brs\.?\s*\d|₹", t, maxsplit=1, flags=re.I)
    name = (m[0].strip() if m and m[0].strip() else (it.get("title") or t[:60])).strip()
    price = re.search(r"(?:rs\.?|₹)\s*([\d,]+)", t, re.I)
    return f"{name} (Rs {price.group(1)})" if price else name

def goto_robust(page, url, tries=3):
    """Load a page without relying on 'networkidle' (this site never goes idle)."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp else None
            try:
                page.wait_for_load_state("load", timeout=20000)
            except Exception:
                pass
            try:
                page.wait_for_function("document.querySelectorAll('a img').length > 3", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            return status
        except Exception as e:
            last = e
            print(f"   attempt {attempt}/{tries} failed for {url}: {str(e).splitlines()[0]}")
            page.wait_for_timeout(5000)
    raise last

def count_products(page):
    return page.evaluate("""() => new Set([...document.querySelectorAll('a[href*="product_overview"] img')]
        .map(i => (i.getAttribute('data-src') || i.currentSrc || i.src))).size""")

def load_all(page):
    """Tick 'Include out of Stock' if present, then click 'Load More' until nothing new appears."""
    for sel in ("text=Include out of Stock", "label:has-text('Include out of Stock')"):
        try:
            page.locator(sel).first.click(timeout=2500)
            page.wait_for_timeout(2000)
            break
        except Exception:
            pass

    btn_sel = ("button:has-text('Load More'), a:has-text('Load More'), "
               "input[value*='Load More' i], div:has-text('Load More'):not(:has(div))")
    clicks, stagnant = 0, 0
    for _ in range(60):
        before = count_products(page)
        btn = page.locator(btn_sel).first
        try:
            visible = btn.count() > 0 and btn.is_visible()
        except Exception:
            visible = False
        if not visible:
            page.mouse.wheel(0, 8000)          # button may only render after scrolling
            page.wait_for_timeout(2500)
            try:
                visible = page.locator(btn_sel).first.is_visible()
            except Exception:
                visible = False
            if not visible:
                break                           # no more Load More button: list is complete
            btn = page.locator(btn_sel).first
        try:
            btn.scroll_into_view_if_needed(timeout=5000)
            btn.click(timeout=5000)
            clicks += 1
        except Exception as e:
            print(f"   could not click Load More: {str(e).splitlines()[0]}")
            stagnant += 1
            if stagnant >= 3:
                break
            page.wait_for_timeout(2000)
            continue
        grew = False
        for _ in range(30):                     # wait up to ~15 s for new products
            page.wait_for_timeout(500)
            if count_products(page) > before:
                grew = True
                break
        page.wait_for_timeout(1000)
        stagnant = 0 if grew else stagnant + 1
        if stagnant >= 2:
            break
    print(f"   Load More clicked {clicks} time(s); {count_products(page)} products loaded")

def diagnose(page, slug, status, attempt):
    """Print what the page looks like when no products were found, and save a screenshot/HTML."""
    try:
        body = " ".join(page.inner_text("body").split())[:300]
        n_links = page.evaluate("document.querySelectorAll('a[href*=\"product_overview\"]').length")
        n_soon = page.evaluate("(document.body.innerText.match(/coming soon/gi) || []).length")
        print(f"   [diag {slug} #{attempt}] http={status} title={page.title()!r} product_links={n_links} 'coming soon' mentions={n_soon}")
        print(f"   [diag] page text starts: {body}")
        Path("debug").mkdir(exist_ok=True)
        page.screenshot(path=f"debug/{slug}-{attempt}.png", full_page=False)
        Path(f"debug/{slug}-{attempt}.html").write_text(page.content()[:400000], encoding="utf-8")
    except Exception as e:
        print(f"   [diag] failed: {e}")

def scrape_listing(page, slug):
    url = f"{BASE}/{slug}"
    print(f"Scanning {url}")
    items = []
    for attempt in range(1, 4):
        status = goto_robust(page, url)
        try:  # products arrive via AJAX after the page opens: wait for the cards themselves
            page.wait_for_selector('a[href*="product_overview"] img', timeout=45000)
        except Exception:
            print(f"   attempt {attempt}/3: no product cards appeared within 45 s")
        load_all(page)
        items = page.evaluate(EXTRACT_JS)
        if items:
            break
        diagnose(page, slug, status, attempt)
        page.wait_for_timeout(10000)
    for it in items:
        it["status"] = classify(it["text"])
        it["slug"] = slug
    return items

def check_product_page(page, url):
    """Authoritative check on the product page. Returns (status, full_title_or_None)."""
    goto_robust(page, url)
    title = None
    try:
        title = page.locator("h1, h2, h3").filter(has_text=re.compile("hmt", re.I)).first.inner_text(timeout=3000).strip()
    except Exception:
        pass
    # Positive evidence first: a visible, enabled Add to Cart button means it can be bought,
    # even if the page mentions "coming soon" elsewhere (related items, banners).
    btn = page.locator("button:has-text('Add to cart'), a:has-text('Add to cart'), input[value*='Add to cart' i]")
    try:
        if btn.count() and btn.first.is_visible() and btn.first.is_enabled():
            return "in_stock", title
    except Exception:
        pass
    body = page.inner_text("body").lower()
    if "coming soon" in body:
        return "coming_soon", title
    if "out of stock" in body or "sold out" in body:
        return "out_of_stock", title
    return "unknown", title

# ---------- notifications ----------
def notify(title, message, url=None):
    sent = False
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        h = {"Title": title.encode("ascii", "ignore").decode(), "Priority": "high", "Tags": "watch,tada"}
        if url:
            h["Click"] = url
        r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), headers=h, timeout=20)
        r.raise_for_status(); sent = True
    if os.getenv("SMTP_HOST") and os.getenv("MAIL_TO"):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, os.getenv("SMTP_USER", ""), os.getenv("MAIL_TO")
        msg.set_content(message + (f"\n\n{url}" if url else ""))
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587"))) as s:
            s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)
        sent = True
    if not sent:
        print("!! No notification channel configured (set NTFY_TOPIC and/or SMTP_*)")
    print(f"[notify] {title} | {message}")

HEALTH_FILE = Path("health.json")
FAIL_ALERT_AT = int(os.getenv("FAIL_ALERT_AT", "6"))   # consecutive failed runs before a phone alert

def record_health(ok):
    try:
        h = json.loads(HEALTH_FILE.read_text()) if HEALTH_FILE.exists() else {"fails": 0}
    except Exception:
        h = {"fails": 0}
    if ok:
        if h.get("fails", 0) >= FAIL_ALERT_AT:
            notify("HMT monitor recovered", "The HMT stock monitor is working again.")
        h["fails"] = 0
    else:
        h["fails"] = h.get("fails", 0) + 1
        if h["fails"] == FAIL_ALERT_AT:
            notify("HMT monitor is failing", f"The last {FAIL_ALERT_AT} checks could not read the site. "
                   "Check the GitHub Actions log.")
    HEALTH_FILE.write_text(json.dumps(h))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if args.test:
        notify("HMT alert test", "If you can read this, alerts work.", BASE)
        return

    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
    if prev and any(not isinstance(v, dict) or "url" not in v for v in prev.values()):
        print("Old-format state.json found; starting a fresh baseline.")
        prev = None
    current, errors = {}, 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/124 Safari/537.36")
        page = ctx.new_page()

        for slug in PAGES:
            try:
                items = scrape_listing(page, slug)
            except Exception as e:
                print(f"!! {slug}: {e}"); errors += 1; continue
            if not items:
                print(f"!! {slug}: found 0 products (site layout changed or blocked?)"); errors += 1; continue
            skipped, dup = 0, {}
            for i, it in enumerate(items):
                nm = name_of(it)
                if EXCLUDE and any(x in (nm + " " + it["img"]).lower() for x in EXCLUDE):
                    skipped += 1
                    continue
                key = it["img"] or f"{slug}|{nm}"
                dup[key] = dup.get(key, 0) + 1
                if dup[key] > 1:
                    key = f"{key}#{dup[key]}"
                current[key] = {"name": nm, "status": it["status"], "slug": slug, "url": it["url"]}
            print(f"{slug}: kept {len(items) - skipped}, skipped {skipped} excluded")

        # Verify on the product page: unknown cards, and any apparent flip to in_stock
        for key, rec in current.items():
            was = (prev or {}).get(key, {}).get("status")
            flipping = rec["status"] == "in_stock" and was in ("coming_soon", "out_of_stock")
            if rec["status"] == "unknown" or flipping:
                try:
                    verified, title = check_product_page(page, rec["url"])
                    if title:
                        rec["name"] = f"{title}"
                    if verified != "unknown":
                        rec["status"] = verified
                    print(f"   verified {rec['name']}: {verified}")
                except Exception as e:
                    print(f"!! verify {rec['url']}: {e}")
        browser.close()

    if args.dump:
        counts = {}
        for key, r in current.items():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            print(f"{r['status']:13} {r['name'][:70]}")
        print("\nSummary:", counts, f"| total {len(current)}")
        return

    if not current:
        record_health(False)
        sys.exit("No products scraped; state left untouched.")
    record_health(errors == 0)

    if prev is None:
        STATE_FILE.write_text(json.dumps(current, indent=2))
        counts = {}
        for r in current.values():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"Baseline saved: {len(current)} products {counts}. Alerts start from the next run.")
        return

    for key, rec in current.items():
        old = prev.get(key)
        if rec["status"] != "in_stock":
            continue
        if old and old["status"] in ("coming_soon", "out_of_stock", "unknown"):
            reason = f"was {old['status'].replace('_', ' ')}"
        elif not old and NOTIFY_NEW:
            reason = "new listing"
        else:
            continue
        if WATCH and not any(w in rec["name"].lower() for w in WATCH):
            continue
        notify(f"IN STOCK: {rec['name']}"[:100], f"{rec['name']} is now in stock ({reason}).", rec["url"])

    # keep records of products missing from this scrape (avoids false 'new' / flip noise later)
    missing = 0
    for key, old in prev.items():
        if key not in current:
            current[key] = old; missing += 1
    if missing:
        print(f"{missing} previously seen product(s) not on page this time (kept in state).")
    STATE_FILE.write_text(json.dumps(current, indent=2))
    print(f"Run complete: {len(current)} products tracked.")
    if errors:
        sys.exit(1)

if __name__ == "__main__":
    main()
