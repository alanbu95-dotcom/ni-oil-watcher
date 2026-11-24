import os
import json
import re
import statistics
import requests
from bs4 import BeautifulSoup

NTFY_TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE_FILE = "price_state.json"

AREA_SLUG = "lisburn"
TARGET_URL = f"https://www.cheapestoil.co.uk/heating-oil-prices/{AREA_SLUG}"
UK_BENCHMARK_URL = "https://www.fueltool.co.uk/heating-oil-prices"
REQUIRED_FLAT_HOURS = 4  # Must hold steady for 4 consecutive checks after a drop

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

def fetch_uk_benchmark():
    """Fail-safe daily UK ex-VAT pence-per-litre benchmark lookup."""
    try:
        resp = requests.get(UK_BENCHMARK_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        # Matches e.g. "98.45p per litre, ex VAT" or "UK average today 98.00p"
        match = re.search(r"(\d{2,3}\.\d{2})\s*p\s*(?:/\s*litre|per\s+litre,?\s*ex\s+VAT|Live\s+average)", text, re.IGNORECASE)
        if not match:
            match = re.search(r"Fueltool\s+average\s+today[^0-9]{0,30}(\d{2,3}\.\d{2})\s*p", text, re.IGNORECASE)
        if match:
            ppl = float(match.group(1))
            if 50.0 <= ppl <= 200.0:
                print(f"[UK Benchmark] {ppl:.2f}p/L ex-VAT")
                return ppl
    except Exception as exc:
        print(f"[UK Benchmark] Lookup skipped: {exc}")
    return None

def fetch_local_market():
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" | ", strip=True)

    bt_match = re.search(r"Showing\s+\d+\s+quotes\s+for\s+(BT\d+)", text, re.IGNORECASE)
    area_label = bt_match.group(1).upper() if bt_match else AREA_SLUG.replace("-", " ").title()

    pattern = re.compile(
        r"\|\s*([^|]{2,45}?)\s*\|\s*Updated\s+[^|]+\|([^|]+(?:\|[^|]+){0,8}?)\|\s*500\s*(?:L|litres):?\s*\|\s*£\s*(\d{3}(?:\.\d{2})?)",
        re.IGNORECASE,
    )

    quotes = []
    seen_suppliers = set()
    for match in pattern.finditer(text):
        supplier = match.group(1).strip()
        middle = match.group(2)
        price = float(match.group(3))

        if "updated" in middle.lower():
            continue
        key = supplier.lower()
        if key in seen_suppliers:
            continue
        if 380.0 <= price <= 750.0 and key not in ("supplier", "500 litres", "delivers to"):
            seen_suppliers.add(key)
            quotes.append((price, supplier))

    if not quotes:
        alt_pattern = re.compile(
            r"500\s*(?:L|litres):?\s*\|?\s*£\s*(\d{3}(?:\.\d{2})?)[^|]*\|[^|]*\|\s*([^|]{2,45}?)\s*\|\s*Updated",
            re.IGNORECASE,
        )
        for match in alt_pattern.finditer(text):
            price = float(match.group(1))
            supplier = match.group(2).strip()
            key = supplier.lower()
            if 380.0 <= price <= 750.0 and key not in seen_suppliers:
                seen_suppliers.add(key)
                quotes.append((price, supplier))

    if not quotes:
        raise RuntimeError(f"Could not parse 500L supplier rows from {TARGET_URL}")

    quotes.sort(key=lambda x: x[0])
    best_price, best_supplier = quotes[0]
    top5_prices = [q[0] for q in quotes[:5]]
    top5_avg = round(statistics.mean(top5_prices), 2)
    median_price = round(statistics.median([q[0] for q in quotes]), 2)

    print(
        f"[{area_label}] {len(quotes)} unique suppliers | Best: £{best_price:.2f} ({best_supplier}) | "
        f"Top-5 Avg: £{top5_avg:.2f} | Median: £{median_price:.2f}"
    )
    return {
        "area": area_label,
        "best_price": best_price,
        "supplier": best_supplier,
        "top5_avg": top5_avg,
        "median": median_price,
        "quote_count": len(quotes),
    }

def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def send_alert(title, message, priority="high", tags="chart_with_upwards_trend"):
    clean_title = title.encode("ascii", "ignore").decode("ascii").strip()
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": clean_title,
            "Priority": priority,
            "Tags": tags,
        },
        timeout=15,
    )
    r.raise_for_status()

def main():
    market = fetch_local_market()
    uk_ppl = fetch_uk_benchmark()
    prev = load_last_state()

    area = market["area"]
    best = market["best_price"]
    supplier = market["supplier"]
    top5 = market["top5_avg"]
    median = market["median"]

    prev_uk = prev.get("uk_ppl")
    if uk_ppl is not None and prev_uk is not None:
        uk_diff = round(uk_ppl - prev_uk, 2)
        uk_dir = "up" if uk_diff > 0 else ("down" if uk_diff < 0 else "flat")
    elif uk_ppl is not None:
        uk_diff = 0.0
        uk_dir = prev.get("uk_dir", "baseline")
    else:
        uk_ppl = prev_uk
        uk_diff = 0.0
        uk_dir = prev.get("uk_dir", "unavailable")

    uk_note = f" | UK Avg: {uk_ppl:.2f}p ({uk_dir})" if uk_ppl is not None else ""

    if not prev or prev.get("area") != area or "uk_ppl" not in prev:
        save_state({
            **market,
            "uk_ppl": uk_ppl,
            "uk_dir": uk_dir,
            "trend": "baseline",
            "flat_hours": 0,
            "floor_alerted": False,
        })
        send_alert(
            f"NI Oil Monitor Upgraded ({area})",
            f"Tracking {market['quote_count']} unique suppliers. Best 500L: £{best:.2f} ({supplier}) | Top-5 Avg: £{top5:.2f} | Median: £{median:.2f}{uk_note}.",
            priority="default",
            tags="white_check_mark",
        )
        return

    prev_best = prev["best_price"]
    prev_top5 = prev.get("top5_avg", top5)
    prev_median = prev.get("median", median)

    best_diff = round(best - prev_best, 2)
    top5_diff = round(top5 - prev_top5, 2)
    median_diff = round(median - prev_median, 2)
    flat_hours = prev.get("flat_hours", 0)
    floor_alerted = prev.get("floor_alerted", False)

    # 1. Reversal / Uptick
    if best_diff > 0 or top5_diff >= 0.50:
        save_state({
            **market,
            "uk_ppl": uk_ppl,
            "uk_dir": uk_dir,
            "trend": "up",
            "flat_hours": 0,
            "floor_alerted": False,
        })
        send_alert(
            f"BUY ALERT ({area}): Reversal at GBP {best:.2f}",
            f"Best 500L: £{prev_best:.2f} -> £{best:.2f} ({supplier}) | Top-5 Avg: £{prev_top5:.2f} -> £{top5:.2f}{uk_note}.",
            priority="urgent",
            tags="rotating_light,chart_with_upwards_trend",
        )

    # 2. Still falling locally
    elif best_diff < 0 or top5_diff <= -0.25:
        save_state({
            **market,
            "uk_ppl": uk_ppl,
            "uk_dir": uk_dir,
            "trend": "down",
            "flat_hours": 0,
            "floor_alerted": False,
        })

    # 3. Holding steady after a downward trajectory
    else:
        if prev.get("trend") in ("down", "stalling"):
            flat_hours += 1
            is_round_number = (round(best % 1, 2) == 0.0)
            median_still_falling = (median_diff <= -0.50)
            uk_still_falling = (uk_dir == "down")

            if flat_hours >= REQUIRED_FLAT_HOURS and not floor_alerted:
                if (is_round_number and median_still_falling) or uk_still_falling:
                    label = f"ARTIFICIAL SHELF ({area}): GBP {best:.2f}"
                    detail = f"Paused {flat_hours}h at £{best:.2f} ({supplier}), but wider market/UK benchmark is still softening."
                else:
                    label = f"CONFIRMED FLOOR ({area}): GBP {best:.2f}"
                    detail = f"Held steady for {flat_hours}h after drop. Best: £{best:.2f} ({supplier}) | Top-5 Avg: £{top5:.2f} | Median: £{median:.2f}."

                save_state({
                    **market,
                    "uk_ppl": uk_ppl,
                    "uk_dir": uk_dir,
                    "trend": "floor",
                    "flat_hours": flat_hours,
                    "floor_alerted": True,
                })
                send_alert(
                    label,
                    f"{detail}{uk_note}",
                    priority="high",
                    tags="pause_button,fuelpump",
                )
            else:
                save_state({
                    **market,
                    "uk_ppl": uk_ppl,
                    "uk_dir": uk_dir,
                    "trend": "stalling",
                    "flat_hours": flat_hours,
                    "floor_alerted": floor_alerted,
                })
        else:
            save_state({
                **market,
                "uk_ppl": uk_ppl,
                "uk_dir": uk_dir,
                "trend": prev.get("trend", "flat"),
                "flat_hours": flat_hours,
                "floor_alerted": floor_alerted,
            })

if __name__ == "__main__":
    main()
