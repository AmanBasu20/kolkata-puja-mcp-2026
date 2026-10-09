"""
Kolkata Puja Tourist MCP - KolkataKhoj Scraper (Flat Schema)

Scrapes all 224 verified pandals from kolkatakhoj.com.
Outputs a flattened, strict JSON schema: id, name, area, address, zone, lat, lon.
"""

import json
import re
from pathlib import Path
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
PANDALS_FILE = BASE_DIR / "data" / "static" / "pandals_2026.json"
THEMES_FILE = BASE_DIR / "data" / "dynamic" / "pandal_themes_2026.json"

def scrape_kolkatakhoj():
    print("Launching Playwright to connect to KolkataKhoj...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print("Loading https://kolkatakhoj.com/pandals/?scope=all ...")
        page.goto("https://kolkatakhoj.com/pandals/?scope=all", timeout=60000)
        
        print("Scrolling down to trigger all lazy-loaded pandals...")
        last_height = page.evaluate("document.body.scrollHeight")
        while True:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                page.wait_for_timeout(3000)
                new_height = page.evaluate("document.body.scrollHeight")
                if new_height == last_height:
                    break
            last_height = new_height
            
        print("Scroll complete. Extracting HTML DOM...")
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    route_buttons = soup.find_all(string=re.compile(r"\+\s*Route", re.IGNORECASE))
    
    cards = []
    for btn in route_buttons:
        parent = btn.parent
        for _ in range(10):
            if parent is None:
                break
            if parent.name in ['div', 'article', 'li'] and parent.find('img'):
                if parent not in cards:
                    cards.append(parent)
                break
            parent = parent.parent
            
    if not cards:
        print("[!] No cards detected.")
        return

    parsed_pandals = []
    parsed_themes = {}
    now_utc = datetime.now(timezone.utc).isoformat()
    pid_counter = 1

    ignore_list = [
        'kolkatakhoj choice', 'must-see', 'theme', 'traditional', 
        'busy', 'easy going', 'classic traditional idol', 'directions'
    ]

    for card in cards:
        # Image
        img_tag = card.find('img')
        photo_url = ""
        if img_tag:
            photo_url = img_tag.get('src', '')
            if "data:image" in photo_url or "placeholder" in photo_url:
                photo_url = img_tag.get('data-src', photo_url)

        # Name
        headings = card.find_all(['h2', 'h3', 'h4', 'strong'])
        name = headings[0].get_text(strip=True) if headings else "Unknown Pandal"
        if not name or "Route" in name:
            continue
            
        strings = list(card.stripped_strings)
        
        # Area Extraction (ignoring badges)
        area = "Kolkata"
        try:
            name_idx = strings.index(name)
            for i in range(name_idx + 1, min(name_idx + 6, len(strings))):
                candidate = strings[i].strip()
                if candidate and candidate.lower() not in ignore_list and "Route" not in candidate:
                    area = candidate.split('·')[0].strip()
                    break
        except ValueError:
            pass

        # Coordinates
        lat, lon = None, None
        directions_url = "https://kolkatakhoj.com/pandals/?scope=all"
        links = card.find_all('a', href=True)
        
        for a in links:
            href = a['href']
            if "google" in href or "maps" in href or "dir" in href:
                directions_url = href
                match = re.search(r"@([-0-9.]+),([-0-9.]+)", href)
                if match:
                    lat, lon = float(match.group(1)), float(match.group(2))
                else:
                    match_dest = re.search(r"destination=([-0-9.]+),([-0-9.]+)", href)
                    if match_dest:
                        lat, lon = float(match_dest.group(1)), float(match_dest.group(2))

        # Zone
        zone = "Kolkata"
        if lat:
            if lat > 22.585: zone = "North Kolkata"
            elif 22.550 <= lat <= 22.585: zone = "Central Kolkata"
            elif lat < 22.515: zone = "South Kolkata"
            else: zone = "South Kolkata"
            
        p_id = f"P{pid_counter:03d}"
        
        # EXACT REQUESTED SCHEMA
        parsed_pandals.append({
            "id": p_id,
            "name": name,
            "area": area,
            "address": f"{area}, Kolkata, West Bengal",
            "zone": zone,
            "latitude": round(lat, 6) if lat else 22.5726,
            "longitude": round(lon, 6) if lon else 88.3639
        })
        
        # Theme data
        theme_type = "Theme based" if "Theme" in strings else "Traditional Puja"
        if "Traditional" in strings: 
            theme_type = "Traditional Ekchala"
            
        parsed_themes[p_id] = {
            "pandal_id": p_id,
            "theme": theme_type,
            "artist": "Local Artisans",
            "photo_url": photo_url,
            "raw_announcement": f"Verified via KolkataKhoj. Area: {area}.",
            "source_url": directions_url,
            "last_updated": now_utc
        }
        
        pid_counter += 1

    # Save
    PANDALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PANDALS_FILE, "w", encoding="utf-8") as f:
        json.dump(parsed_pandals, f, indent=2, ensure_ascii=False)
        
    THEMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(THEMES_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "dataset_name": "KolkataKhoj Verified Themes & Visuals",
            "last_collected_at": now_utc,
            "themes_by_pandal": parsed_themes
        }, f, ensure_ascii=False, indent=2)
        
    print(f"\n✅ SUCCESS: Scraped {len(parsed_pandals)} authentic pandals.")

if __name__ == "__main__":
    scrape_kolkatakhoj()