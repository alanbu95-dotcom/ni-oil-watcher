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
REQUIRED_FLAT_HOURS = 4  # Must hold steady for 4 consecutive checks after a drop

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

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
    for match in pattern.finditer(text):
        supplier = match.group(1).strip()
        middle = match.group(2)
        price = float(match.group(3))

        if "updated" in middle.lower():
            continue
        if 380.0 <= price <= 750.0 and supplier.lower() not in ("supplier", "500 litres", "delivers to"):
            quotes.append((price, supplier))

    if not quotes:
        alt_pattern = re.compile(
            r"500\s*(?:L|litres):?\s*\|?\s*£\s*(\d{3}(?:\.\d{2})?)[^|]*\|[^|]*\|\s*([^|]{2,45}?)\s*\|\s*Updated",
            re.IGNORECASE,
        )
        for match in alt_pattern.finditer(text):
            price = float(match.group(1))
            supplier = match.group(2).strip()
            if 380.0 <= price <= 750.0:
                quotes.append((price, supplier))

    if not quotes:
        raise RuntimeError(f"Could not parse 500L supplier rows from {TARGET_URL}")

    quotes.sort(key=lambda x: x[0])
    best_price, best_supplier = quotes[0]
    top5_prices = [q[0] for q in quotes[:5]]
    top5_avg = round(statistics.mean(top5_prices), 2)
    median_price = round(statistics.median([q[0] for q in quotes]), 2)

    print(
        f"[{area_label}] {len(quotes)} quotes | Best: £{best_price:.2f} ({best_supplier}) | "
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
    # Keep Title strictly ASCII so ntfy never renders replacement characters
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
    prev = load_last_state()

    area = market["area"]
    best = market["best_price"]
    supplier = market["supplier"]
    top5 = market["top5_avg"]
    median = market["median"]

    if not prev or prev.get("area") != area or "best_price" not in prev:
        save_state({
            **market,
            "trend": "baseline",
            "flat_hours": 0,
            "floor_alerted": False,
        })
        send_alert(
            f"NI Oil Monitor Upgraded ({area})",
            f"Tracking {market['quote_count']} suppliers. Best 500L: £{best:.2f} ({supplier}) | Top-5 Avg: £{top5:.2f} | Median: £{median:.2f}.",
            priority="default",
            tags="white_check_mark",
        )
        return

    prev_best = prev["best_price"]
    prev_top5 = prev.get("top5_avg", top5)
    best_diff = round(best - prev_best, 2)
    top5_diff = round(top5 - prev_top5, 2)
    flat_hours = prev.get("flat_hours", 0)
    floor_alerted = prev.get("floor_alerted", False)

    # 1. Reversal / Uptick: either the best quote rises OR the top-5 average rises by >= £0.50
    if best_diff > 0 or top5_diff >= 0.50:
        save_state({
            **market,
            "trend": "up",
            "flat_hours": 0,
            "floor_alerted": False,
        })
        send_alert(
            f"BUY ALERT ({area}): Reversal at GBP {best:.2f}",
            f"Best 500L: £{prev_best:.2f} -> £{best:.2f} ({supplier}) | Top-5 Avg: £{prev_top5:.2f} -> £{top5:.2f}.",
            priority="urgent",
            tags="rotating_light,chart_with_upwards_trend",
        )

    # 2. Still falling: either best quote dropped OR broader top-5 average dropped by >= £0.25
    elif best_diff < 0 or top5_diff <= -0.25:
        save_state({
            **market,
            "trend": "down",
            "flat_hours": 0,
            "floor_alerted": False,
        })

    # 3. Holding steady after a downward trajectory
    else:
        if prev.get("trend") in ("down", "stalling"):
            flat_hours += 1
            if flat_hours >= REQUIRED_FLAT_HOURS and not floor_alerted:
                save_state({
                    **market,
                    "trend": "floor",
                    "flat_hours": flat_hours,
                    "floor_alerted": True,
                })
                send_alert(
                    f"CONFIRMED FLOOR ({area}): GBP {best:.2f}",
                    f"Held steady for {flat_hours}h after drop. Best: £{best:.2f} ({supplier}) | Top-5 Avg: £{top5:.2f} | Median: £{median:.2f}.",
                    priority="high",
                    tags="pause_button,fuelpump",
                )
            else:
                save_state({
                    **market,
                    "trend": "stalling",
                    "flat_hours": flat_hours,
                    "floor_alerted": floor_alerted,
                })
        else:
            save_state({
                **market,
                "trend": prev.get("trend", "flat"),
                "flat_hours": flat_hours,
                "floor_alerted": floor_alerted,
            })

if __name__ == "__main__":
    main()
