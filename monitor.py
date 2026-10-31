#!/usr/bin/env python3
"""
HMT Watches stock alert.
Renders hmtwatches.in listing pages in a headless browser, tracks each product's
status (coming_soon / out_of_stock / in_stock) and notifies you when a product
that was NOT in stock becomes in stock.

Usage:
  python monitor.py --dump     # show what it detects, no state saved, no alerts
  python monitor.py            # normal run (first run only records a baseline)
  python monitor.py --test     # send a test notification

Config via environment variables:
  NTFY_TOPIC      ntfy.sh topic -> push to phone (install the ntfy app, subscribe)
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, MAIL_TO   -> email (optional)
  HMT_PAGES       comma list, default: mens,womens,pair,unisex
  HMT_WATCH       optional comma list of keywords; only alert for names containing one
                  e.g. "gandaberunda,vijay"
  NOTIFY_NEW      "1" to also alert on brand-new listings that appear already in stock
  STATE_FILE      default state.json
"""
import argparse, json, os, re, smtplib, sys, time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

BASE = "https://www.hmtwatches.in"
PAGES = [p.strip() for p in os.getenv("HMT_PAGES", "mens,womens,pair,unisex").split(",") if p.strip()]
WATCH = [w.strip().lower() for w in os.getenv("HMT_WATCH", "").split(",") if w.strip()]
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
NOTIFY_NEW = os.getenv("NOTIFY_NEW") == "1"

# Anchors on the listing page that wrap a product image; climbs to the card boundary.
EXTRACT_JS = r"""
() => {
  const skip = /logo|facebook|twitter|instagram|youtube/i;
  const anchors = [...document.querySelectorAll('a[href]')].filter(a => {
    const img = a.querySelector('img');
    return img && !skip.test(img.src) && !skip.test(a.href) && !a.href.endsWith('#') && a.href.startsWith(location.origin);
  });
  const hrefs = new Set(anchors.map(a => a.href));
  const out = {};
  for (const a of anchors) {
    let card = a, last = a;
    while (card.parentElement) {
      const p = card.parentElement;
      const n = new Set([...p.querySelectorAll('a[href]')].map(x => x.href).filter(h => hrefs.has(h)));
      if (n.size > 1) break;
      card = p; 
    }
    const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
    if (!out[a.href] || text.length > out[a.href].text.length) {
      out[a.href] = { url: a.href, text, title: (a.title || a.querySelector('img').alt || '').trim() };
    }
  }
  return Object.values(out);
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
    return "unknown"

def load_all(page):
    """Scroll / click 'load more' so lazy-loaded products appear."""
    for sel in ("text=Include out of Stock", "label:has-text('Include out of Stock')"):
        try:
            page.locator(sel).first.click(timeout=2500)
            page.wait_for_timeout(1500)
            break
        except Exception:
            pass
    prev = -1
    for _ in range(25):
        page.mouse.wheel(0, 6000)
        page.wait_for_timeout(700)
        for sel in ("text=/load more|view more|show more/i",):
            try:
                page.locator(sel).first.click(timeout=500)
            except Exception:
                pass
        n = page.evaluate("document.querySelectorAll('img').length")
        if n == prev:
            break
        prev = n

def scrape_listing(page, slug):
    page.goto(f"{BASE}/{slug}", wait_until="networkidle", timeout=60000)
    load_all(page)
    items = page.evaluate(EXTRACT_JS)
    for it in items:
        it["status"] = classify(it["text"])
        it["slug"] = slug
    return items

def check_product_page(page, url):
    """Authoritative check on the product page itself."""
    page.goto(url, wait_until="networkidle", timeout=60000)
    body = page.inner_text("body").lower()
    if "coming soon" in body:
        return "coming_soon"
    if "out of stock" in body or "sold out" in body:
        return "out_of_stock"
    btn = page.locator("button:has-text('Add to cart'), a:has-text('Add to cart'), input[value*='Add to cart' i]")
    try:
        if btn.count() and btn.first.is_visible() and btn.first.is_enabled():
            return "in_stock"
    except Exception:
        pass
    return "unknown"

def name_of(it):
    if it.get("title"):
        return it["title"]
    m = re.match(r"(.{5,80}?)(MRP|₹|Rs)", it["text"])
    return (m.group(1) if m else it["text"][:70]).strip()

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if args.test:
        notify("HMT alert test", "If you can read this, alerts work.", BASE)
        return

    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
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
            for it in items:
                current[it["url"]] = {"name": name_of(it), "status": it["status"], "slug": slug}

        # Resolve 'unknown' cards, and double-check any apparent flip to in_stock
        for url, rec in current.items():
            was = (prev or {}).get(url, {}).get("status")
            flipping = rec["status"] == "in_stock" and was in ("coming_soon", "out_of_stock")
            if rec["status"] == "unknown" or flipping:
                try:
                    verified = check_product_page(page, url)
                    if verified != "unknown":
                        rec["status"] = verified
                except Exception as e:
                    print(f"!! verify {url}: {e}")
        browser.close()

    if args.dump:
        counts = {}
        for url, r in current.items():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            print(f"{r['status']:13} {r['name'][:60]:60} {url}")
        print("\nSummary:", counts, f"| total {len(current)}")
        return

    if not current:
        sys.exit("No products scraped; state left untouched.")

    if prev is None:
        STATE_FILE.write_text(json.dumps(current, indent=2))
        print(f"Baseline saved: {len(current)} products. Alerts start from the next run.")
        return

    for url, rec in current.items():
        old = prev.get(url)
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
        notify(f"IN STOCK: {rec['name']}"[:100], f"{rec['name']} is now in stock ({reason}).", url)

    # keep records of products that vanished from this scrape (avoids false 'new' later)
    for url, old in prev.items():
        current.setdefault(url, old)
    STATE_FILE.write_text(json.dumps(current, indent=2))
    if errors:
        sys.exit(1)

if __name__ == "__main__":
    main()
