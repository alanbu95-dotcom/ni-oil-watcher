import os
import json
import re
import requests
from bs4 import BeautifulSoup

NTFY_TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE_FILE = "price_state.json"

SOURCES = [
    ("CheapestOil NI", "https://www.cheapestoil.co.uk/Heating-Oil-NI"),
    ("OilCompare NI", "https://oilcompare.co.uk/northern-ireland"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

def fetch_current_price():
    errors = []
    for name, url in SOURCES:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            print(f"[{name}] HTTP {resp.status_code}")
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(" ", strip=True)

            # Look for explicit 500L quote patterns (e.g. "500 litres from £480" or "500 litres | 96.8p | £484")
            m500 = re.search(r"500\s*litres?[^£]{0,30}£\s*(\d{3}(?:\.\d{2})?)", text, re.IGNORECASE)
            if m500:
                price = float(m500.group(1))
                if 350.0 <= price <= 750.0:
                    print(f"[{name}] Matched 500L quote: £{price:.2f}")
                    return price, name

            # Fallback: scan for any 500L-range GBP quote (£380 - £750)
            matches = re.findall(r"£\s*(\d{3}(?:\.\d{2})?)", text)
            prices = [float(p) for p in matches if 380.0 <= float(p) <= 750.0]
            if prices:
                price = min(prices)
                print(f"[{name}] Matched lowest GBP quote: £{price:.2f}")
                return price, name

            errors.append(f"{name}: Page loaded but no £380-£750 quote found")
        except Exception as e:
            print(f"[{name}] Failed: {e}")
            errors.append(f"{name}: {e}")

    raise RuntimeError("All sources failed -> " + " | ".join(errors))

def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_price": None, "trend": "unknown"}

def save_state(price, trend, source):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_price": price, "trend": trend, "source": source}, f, indent=2)

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
    current_price, source = fetch_current_price()
    state = load_last_state()
    last_price = state["last_price"]

    if last_price is None:
        save_state(current_price, "baseline", source)
        send_alert(
            "NI Oil Monitor Active",
            f"Baseline 500L lowest NI quote recorded at £{current_price:.2f} (via {source}).",
            priority="default",
            tags="white_check_mark",
        )
        return

    diff = round(current_price - last_price, 2)

    if diff > 0:
        save_state(current_price, "up", source)
        send_alert(
            f"BUY ALERT: NI Oil Up +£{diff:.2f}",
            f"Lowest 500L quote rose from £{last_price:.2f} to £{current_price:.2f} (via {source}).",
            priority="urgent",
            tags="rotating_light,chart_with_upwards_trend",
        )
    elif diff == 0 and state["trend"] == "down":
        save_state(current_price, "flat", source)
        send_alert(
            f"FLOOR DETECTED: NI Oil Holding at £{current_price:.2f}",
            f"Price stopped dropping and held steady at £{current_price:.2f} (via {source}).",
            priority="high",
            tags="pause_button,fuelpump",
        )
    elif diff < 0:
        save_state(current_price, "down", source)
    else:
        save_state(current_price, "flat", source)

if __name__ == "__main__":
    main()
    
