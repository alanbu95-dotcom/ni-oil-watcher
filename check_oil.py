import os
import json
import re
import requests
from bs4 import BeautifulSoup

NTFY_TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE_FILE = "price_state.json"

# Set your town slug on CheapestOil (e.g., "lisburn", "belfast", "bangor", "newry", "ballymena")
AREA_SLUG = "lisburn"
TARGET_URL = f"https://www.cheapestoil.co.uk/heating-oil-prices/{AREA_SLUG}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

def fetch_local_best_quote():
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
    print(f"[{AREA_SLUG.title()}] HTTP {resp.status_code}")
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" | ", strip=True)

    # Extract postcode label (e.g., "Showing 23 quotes for BT28")
    bt_match = re.search(r"Showing\s+\d+\s+quotes\s+for\s+(BT\d+)", text, re.IGNORECASE)
    area_label = bt_match.group(1).upper() if bt_match else AREA_SLUG.replace("-", " ").title()

    # Match each supplier block:
    # "| Port Fuels | Updated 40 minutes ago | <any columns without £ or Updated> | £491.99"
    pattern = re.compile(
        r"\|\s*([^|]{2,45}?)\s*\|\s*Updated\s+[^|]+\|([^£]{1,140}?)£\s*(\d{3}(?:\.\d{2})?)",
        re.IGNORECASE,
    )

    quotes = []
    for match in pattern.finditer(text):
        supplier = match.group(1).strip()
        middle = match.group(2)
        price = float(match.group(3))

        # Ensure we didn't cross into the next supplier row
        if "updated" in middle.lower():
            continue
        if 350.0 <= price <= 750.0 and supplier.lower() not in ("supplier", "500 litres", "delivers to"):
            quotes.append((price, supplier))

    if not quotes:
        # Print the exact table portion so we can see it in logs if anything ever changes
        idx = text.find("Showing ")
        print("Table snippet:", text[idx:idx+700] if idx != -1 else text[:700])
        raise RuntimeError(f"Could not parse supplier rows from {TARGET_URL}")

    quotes.sort(key=lambda x: x[0])
    best_price, best_supplier = quotes[0]
    print(f"Found {len(quotes)} quotes for {area_label}. Cheapest: {best_supplier} at £{best_price:.2f}")
    return best_price, best_supplier, area_label

def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_price": None, "trend": "unknown", "supplier": None}

def save_state(price, trend, supplier, area_label):
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "area": area_label,
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
    current_price, supplier, area_label = fetch_local_best_quote()
    state = load_last_state()
    last_price = state.get("last_price")

    if last_price is None or state.get("area") != area_label:
        save_state(current_price, "baseline", supplier, area_label)
        send_alert(
            f"NI Oil Monitor Active ({area_label})",
            f"Cheapest 500L in {area_label}: £{current_price:.2f} with {supplier}.",
            priority="default",
            tags="white_check_mark",
        )
        return

    diff = round(current_price - last_price, 2)

    if diff > 0:
        save_state(current_price, "up", supplier, area_label)
        send_alert(
            f"BUY ALERT ({area_label}): Up +£{diff:.2f}",
            f"Cheapest 500L rose from £{last_price:.2f} to £{current_price:.2f} ({supplier}). Reversal detected!",
            priority="urgent",
            tags="rotating_light,chart_with_upwards_trend",
        )
    elif diff == 0 and state.get("trend") == "down":
        save_state(current_price, "flat", supplier, area_label)
        send_alert(
            f"FLOOR DETECTED ({area_label}): £{current_price:.2f}",
            f"Price stopped falling and held at £{current_price:.2f} with {supplier}.",
            priority="high",
            tags="pause_button,fuelpump",
        )
    elif diff < 0:
        save_state(current_price, "down", supplier, area_label)
    else:
        save_state(current_price, "flat", supplier, area_label)

if __name__ == "__main__":
    main()
    
