import os
import json
import re
import requests
from bs4 import BeautifulSoup

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = "price_state.json"

TARGET_URL = "https://www.cheapestheatingoil.co.uk/northern-ireland/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def fetch_current_price():
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    
    matches = re.findall(r"£\s*(\d{3}(?:\.\d{2})?)", text)
    prices = [float(p) for p in matches if 350.0 <= float(p) <= 750.0]
    
    if not prices:
        raise ValueError("Could not parse a valid 500L GBP price from page.")
    return min(prices)

def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_price": None, "trend": "unknown"}

def save_state(price, trend):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_price": price, "trend": trend}, f, indent=2)

def send_alert(title, message, priority="high", tags="chart_with_upwards_trend"):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
        },
        timeout=15,
    )

def main():
    current_price = fetch_current_price()
    state = load_last_state()
    last_price = state["last_price"]

    if last_price is None:
        save_state(current_price, "baseline")
        send_alert(
            "NI Oil Monitor Active",
            f"Baseline 500L lowest quote recorded at £{current_price:.2f}.",
            priority="default",
            tags="white_check_mark",
        )
        return

    diff = round(current_price - last_price, 2)

    if diff > 0:
        save_state(current_price, "up")
        send_alert(
            f"BUY ALERT: NI Oil Up +£{diff:.2f}",
            f"Lowest 500L quote rose from £{last_price:.2f} to £{current_price:.2f}. Reversal detected!",
            priority="urgent",
            tags="rotating_light,chart_with_upwards_trend",
        )
    elif diff == 0 and state["trend"] == "down":
        save_state(current_price, "flat")
        send_alert(
            f"FLOOR DETECTED: NI Oil Holding at £{current_price:.2f}",
            f"Price stopped dropping and held steady at £{current_price:.2f} over the last check.",
            priority="high",
            tags="pause_button,fuelpump",
        )
    elif diff < 0:
        save_state(current_price, "down")
    else:
        save_state(current_price, "flat")

if __name__ == "__main__":
    main()
  
