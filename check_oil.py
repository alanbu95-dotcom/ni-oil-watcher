import os
import json
import re
import requests
from bs4 import BeautifulSoup

NTFY_TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE_FILE = "price_state.json"

# 1. Set your town slug (e.g., "lisburn", "belfast", "bangor", "newry", "ballymena", "antrim")
AREA_SLUG = "lisburn"
# 2. Optional: Set your BT postcode district (e.g., "BT28", "BT1", "BT19") or leave "" for all town suppliers
MY_POSTCODE_PREFIX = "BT28"

TARGET_URL = f"https://www.cheapestoil.co.uk/heating-oil-prices/{AREA_SLUG}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

def postcode_matches(bt_line, target_bt):
    """Checks if target_bt (e.g. 'BT28') is covered by a string like 'BT1 to BT30, BT36 to BT39'."""
    if not target_bt:
        return True
    m = re.match(r"BT(\d+)", target_bt.strip().upper())
    if not m:
        return True
    num = int(m.group(1))
    bt_upper = bt_line.upper()

    # Check ranges like "BT1 TO BT30"
    for start, end in re.findall(r"BT(\d+)\s*TO\s*BT(\d+)", bt_upper):
        if int(start) <= num <= int(end):
            return True
    # Check individual codes like "BT28"
    singles = [int(x) for x in re.findall(r"BT(\d+)", bt_upper)]
    return num in singles

def fetch_local_best_quote():
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
    print(f"[{AREA_SLUG.title()}] HTTP {resp.status_code}")
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" | ", strip=True)

    # Actual CheapestOil town page structure:
    # "Port Fuels | Updated 3 days ago | BT1 to BT30, BT36 to BT39 | 300 litres: £296.99 | 99 ppl | 500 litres: £491.99"
    pattern = re.compile(
        r"([^|]{3,45}?)\s*\|\s*Updated\s+[^|]+\|\s*(BT[^|]+)\|\s*300\s*litres:\s*£[\d,.]+\s*\|\s*[\d.]+\s*ppl\s*\|\s*500\s*litres:\s*£\s*(\d{3}(?:\.\d{2})?)",
        re.IGNORECASE,
    )

    quotes = []
    for match in pattern.finditer(text):
        supplier = match.group(1).strip(" *•-")
        bt_coverage = match.group(2).strip()
        price = float(match.group(3))

        if 350.0 <= price <= 750.0 and postcode_matches(bt_coverage, MY_POSTCODE_PREFIX):
            quotes.append((price, supplier))

    if not quotes:
        # Print a snippet of the page text to logs if format ever changes
        print("Page snippet:", text[:600])
        raise RuntimeError(f"No matching supplier quotes found on {TARGET_URL} for {MY_POSTCODE_PREFIX or AREA_SLUG}")

    quotes.sort(key=lambda x: x[0])
    best_price, best_supplier = quotes[0]
    print(f"Matched {len(quotes)} suppliers for {MY_POSTCODE_PREFIX or AREA_SLUG}. Cheapest: {best_supplier} at £{best_price:.2f}")
    return best_price, best_supplier

def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_price": None, "trend": "unknown", "supplier": None}

def save_state(price, trend, supplier):
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "area": AREA_SLUG,
                "postcode": MY_POSTCODE_PREFIX,
                "last_price": price,
                "supplier": supplier,
                "trend": trend,
            },
            f,
            indent=2,
        )

def send_alert(title, message, priority="high", tags="chart_with_upwards_trend"):
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
        },
        timeout=15,
    )
    r.raise_for_status()

def main():
    current_price, supplier = fetch_local_best_quote()
    state = load_last_state()
    last_price = state.get("last_price")
    area_label = MY_POSTCODE_PREFIX or AREA_SLUG.replace("-", " ").title()

    if last_price is None or state.get("area") != AREA_SLUG or state.get("postcode") != MY_POSTCODE_PREFIX:
        save_state(current_price, "baseline", supplier)
        send_alert(
            f"NI Oil Monitor Active ({area_label})",
            f"Cheapest 500L in {area_label}: £{current_price:.2f} with {supplier}.",
            priority="default",
            tags="white_check_mark",
        )
        return

    diff = round(current_price - last_price, 2)

    if diff > 0:
        save_state(current_price, "up", supplier)
        send_alert(
            f"BUY ALERT ({area_label}): Up +£{diff:.2f}",
            f"Cheapest 500L rose from £{last_price:.2f} to £{current_price:.2f} ({supplier}). Reversal detected!",
            priority="urgent",
            tags="rotating_light,chart_with_upwards_trend",
        )
    elif diff == 0 and state.get("trend") == "down":
        save_state(current_price, "flat", supplier)
        send_alert(
            f"FLOOR DETECTED ({area_label}): £{current_price:.2f}",
            f"Price stopped falling and held at £{current_price:.2f} with {supplier}.",
            priority="high",
            tags="pause_button,fuelpump",
        )
    elif diff < 0:
        save_state(current_price, "down", supplier)
    else:
        save_state(current_price, "flat", supplier)

if __name__ == "__main__":
    main()
    
