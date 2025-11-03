# --------------------------------------------------------------
# real_time_scraper.py
# Live monitor for Pacific Coast JDM auctions – WhatsApp alerts
# --------------------------------------------------------------

import time
import requests
from io import BytesIO
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from bs4 import BeautifulSoup
import os
import re
from twilio.rest import Client
import openai

# === YOUR OPENAI API KEY ===
openai.api_key = "sk-your-real-key-here"  # Get: https://platform.openai.com/api-keys

# ------------------- USER SETTINGS -------------------------
TWILIO_SID   = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_TOKEN = "[Your Twilio Token]"
TWILIO_FROM  = "whatsapp:+1xxxxxxxxxxx"
YOUR_PHONE   = "whatsapp:+1xxxxxxxxxxx"
FRIEND_PHONE = "whatsapp:+xxxxxxxxxxxx"
RECIPIENTS   = [FRIEND_PHONE,YOUR_PHONE]
YOUR_EMAIL = "[Your Email]"
YOUR_PASS  = "[Your Password]"

MAX_HEIGHT_MM = 2065  # Garage fit

SEEN_FILE = "seen_lots.txt"
# -----------------------------------------------------------

# Load seen
if os.path.exists(SEEN_FILE):
    with open(SEEN_FILE, "r") as f:
        seen_ids = set(line.strip() for line in f)
else:
    seen_ids = set()

# Twilio
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# Selenium
service = Service("./chromedriver")
options = Options()
options.add_argument("--disable-cache")
options.add_argument("--disable-application-cache")
options.add_argument("--headless")
driver = webdriver.Chrome(service=service, options=options)

def analyze_report_image_with_gpt4o(image_url):
    try:
        print(f"    → Sending image to GPT-4o Vision: {image_url}")
        
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional JDM auction inspector. Analyze the Japanese inspection report image. Extract ONLY the damage notes. Translate to clear, professional English. List each issue as a bullet point. Ignore specs, scores, or auction info."
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What damage is shown in this inspection report?"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        
        result = response.choices[0].message.content.strip()
        print(f"    → GPT-4o Damage Report:\n{result}")
        return result
        
    except Exception as e:
        error_msg = f"GPT-4o error: {str(e)[:50]}"
        print(error_msg)
        return error_msg


# ------------------- HELPER FUNCTIONS ----------------------
def login():
    print("Logging in...")
    driver.get("https://auction.pacificcoastjdm.com/")
    time.sleep(4)
    driver.find_element(By.NAME, "username").send_keys(YOUR_EMAIL)
    driver.find_element(By.NAME, "password").send_keys(YOUR_PASS)
    driver.find_element(By.NAME, "Submit").click()
    time.sleep(6)
    print("Login OK")

def select_saved_search(search_name):
    print(f"\nChecking: {search_name}")
    # Clear cache by navigating to a fresh URL with timestamp
    driver.get(f"https://auction.pacificcoastjdm.com/auctions/?p=project/searchform&searchtype=max&s&ld&_={int(time.time())}")
    time.sleep(5)
    Select(driver.find_element(By.NAME, "search_id")).select_by_visible_text(search_name)
    time.sleep(2)
    driver.execute_script("startSearch('btnSearsh', searchform);")
    time.sleep(7)
    # Force refresh to get latest results (clear cache)
    driver.refresh()
    time.sleep(5)
    print("Results loaded")

def get_lot_links():
    """
    Collect lot links from all pages of search results.
    Returns list of (lot_id, url) tuples for unseen lots only.
    """
    all_links = {}
    total_found = 0
    page = 1
    
    while True:
        print(f"    Collecting links from page {page}...")
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Extract links from current page
        page_lots = 0
        for row in soup.select("tr.ColorGreed1, tr.ColorGreed2"):
            lot_cell = row.find("td", id=re.compile(r"^bid_number_"))
            if not lot_cell: continue
            a_tag = lot_cell.find("a")
            if not a_tag: continue
            href = a_tag.get("href", "")
            if not href.startswith("/auctions/?p=project/lot"): continue
            full_url = "https://auction.pacificcoastjdm.com" + href
            lot_id_match = re.search(r"id=(\d+)", href)
            lot_id = lot_id_match.group(1) if lot_id_match else None
            if lot_id:
                total_found += 1
                if lot_id not in seen_ids:
                    all_links[lot_id] = full_url
                    page_lots += 1
        
        print(f"      Found {page_lots} new lots on page {page}")
        
        # Try to navigate to next page
        try:
            next_btn = driver.find_element(By.XPATH, "//a[contains(text(), 'Next')]")
            if next_btn.is_enabled() and next_btn.is_displayed():
                next_btn.click()
                time.sleep(3)
                page += 1
            else:
                break
        except Exception:
            break
    
    print(f"    Total: {total_found} lots found, {len(all_links)} new lots across {page} page(s)")
    return [(lid, url) for lid, url in all_links.items()]

def get_lot_details(lot_url):
    print(f"  → {lot_url.split('id=')[-1]}")
    driver.get(lot_url)
    time.sleep(6)
    soup = BeautifulSoup(driver.page_source, "html.parser")

    # Extract specs from table
    rows = soup.select("table.Verdana12px tr")
    data = {}
    for row in rows:
        cells = row.find_all("td", class_="ColorCell_1")
        for cell in cells:
            label = cell.get_text(strip=True).rstrip(":")
            value_cell = cell.find_next_sibling("td")
            if value_cell:
                value = value_cell.get_text(strip=True)
                data[label] = value

    # === Extract Exterior Score (from "Scores" field) ===
    scores_field = data.get("Scores", "N/A")
    overall_score = "N/A"
    exterior = "N/A"
    if scores_field != "N/A" and "/" in scores_field:
        parts = scores_field.split("/")
        overall_score = parts[0].strip()
        if len(parts) > 1:
            exterior = parts[1].strip()
    else:
        overall_score = scores_field

    # === Report Image (Full Size for GPT-4o) ===
    report_img = soup.find("img", id="url_img_0")
    report_url = None
    if report_img:
        if "load_src" in report_img.attrs:
            report_url = report_img["load_src"]
        elif "src" in report_img.attrs:
            report_url = report_img["src"]

    # === GPT-4o Vision: Analyze Image Directly ===
    inspection_notes = "No report image found."
    if report_url:
        print(f"    → OCR on: {report_url}")
        try:
            # Remove thumbnail (h=96) → full image
            if "h=96" in report_url:
                report_url = report_url.replace("&h=96", "").replace("h=96", "")
            
            inspection_notes = analyze_report_image_with_gpt4o(report_url)
        except Exception as e:
            inspection_notes = f"GPT-4o error: {str(e)[:50]}"

    # === Main Photo (for WhatsApp) ===
    main_img = soup.find("img", src=re.compile(r"/1\.jpg"))
    photo = "N/A"
    if main_img:
        if "load_src" in main_img.attrs:
            photo = main_img["load_src"]
        elif "src" in main_img.attrs:
            photo = main_img["src"]

    # === RETURN CLEAN, COMPACT DATA ===
    return {
        "model": data.get("Grade", "HIACE VAN"),
        "mileage": data.get("Mileage, km.", "N/A").replace(" ", ""),
        "scores": overall_score,                    # e.g. 3.5
        "interior": data.get("Interior score", "N/A"),
        "exterior": data.get("Exterior score", "N/A"),                       # e.g. E
        "fuel": data.get("fuel", "N/A").capitalize()[:3],  # Gas, Die, Hyb
        "start_price": data.get("Start price", "N/A").replace(" ", "").replace("JPY*", "").replace("JPY", "").strip(),
        "report_summary": inspection_notes,
        "photo": photo,
        "link": lot_url
    }
def send_summary_whatsapp(vans):
    if not vans:
        return

    photo = vans[0]["photo"]  # Only first van's photo (optional)

    for i, van in enumerate(vans, 1):
        msg = f"*VAN #{i}*: {van['model']} | {van['mileage']}km | {van['scores']}/{van['interior']}/{van['exterior']} | {van['fuel'][:3]} | ¥{van['start_price']}"
        msg += f"\n{van['link']}\n"
        
        if van['report_summary'] and "error" not in van['report_summary'].lower():
            # Include full damage report
            damage = van['report_summary'].strip()
            msg += f"   *DAMAGE:*\n{damage}\n"
        
        msg += "\nGood Luck!"

               # Send one message per van to all recipients
        for recipient in RECIPIENTS:
            try:
                # Send photo only on first van and first recipient (YOUR_PHONE) to avoid spam
                send_photo = (i == 1 and recipient == YOUR_PHONE and photo != "N/A")
                
                twilio_client.messages.create(
                    body=msg,
                    from_=TWILIO_FROM,
                    to=recipient,
                    media_url=[photo] if send_photo else None
                )
                print(f"Sent WhatsApp for van #{i} to {recipient}")
                time.sleep(2)  # Avoid rate limit
            except Exception as e:
                print(f"Failed to send WhatsApp for van #{i} to {recipient}: {e}")

    print(f"SENT {len(vans)} INDIVIDUAL MESSAGES")

# ------------------- MAIN LOOP -----------------------------
try:
    login()
    SEARCHES = ["Toyota Hiace Van"]

    new_vans = []
    for search_name in SEARCHES:
        select_saved_search(search_name)
        lot_links = get_lot_links()
        print(f"Found {len(lot_links)} new lots")
        for lot_id, url in lot_links:
            details = get_lot_details(url)
            new_vans.append(details)
            seen_ids.add(lot_id)

    if new_vans:
        send_summary_whatsapp(new_vans)
    else:
        print("No new vans.")

    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(seen_ids))

except KeyboardInterrupt:
    print("\nStopped.")
finally:
    driver.quit()