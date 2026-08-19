"""
=============================================================================
scraper.py
=============================================================================
Purpose:
This is the main scraper script for cross-referencing hotels between Bookme.pk 
and Agoda.com in Dubai. 

Architecture & Flow:
1. Bookme Indexing: Fetches raw hotel data from the Bookme API (using a search 
   payload for Dubai) to get a list of hotel names and reference IDs. It polls
   the Bookme endpoint multiple times to get a sufficient batch of hotels, but 
   DOES NOT fetch the rooms yet (saving API calls).
2. Agoda Slug Routing: Takes the Bookme hotel name, converts it into an Agoda 
   URL slug format (e.g. hyatt-regency -> hyatt-regency), and pings Agoda. 
   If Agoda returns a direct property URL, it's a match!
3. Agoda Room Extraction: Uses headless Playwright Chromium to navigate to the 
   Agoda property page and extract the listed room names and their images.
4. Bookme Room Extraction: ONLY if Agoda successfully matched and loaded rooms,
   the script will fetch the specific rooms for that hotel from Bookme using 
   the 'single-itinerary' API endpoint.
5. Storage: Progressively saves matched hotels to 'dubai_hotels_matched.json'.
=============================================================================
"""
import asyncio
import json
import datetime
import requests
import urllib.parse
import time
import re
from playwright.async_api import async_playwright

def get_bookme_hotels_dubai():
    print("Initializing Bookme API Extraction...")
    
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # 1. Fetch token via cookies
    print("Fetching Bookme API token...")
    session.get("https://bookme.pk/book-hotels-online", headers=headers)
    api_token_cookie = session.cookies.get("api_token")
    if not api_token_cookie:
        print("Failed to get API token!")
        return []
        
    token = urllib.parse.unquote(api_token_cookie)
    headers["Authorization"] = f"Bearer {token}"
    
    # 2. Initiate Search
    print("Initiating Search for Dubai...")
    search_url = "https://api.bookmesky.com/hotels/api/search"
    payload = {
        "Place": {
            "Type": "geo",
            "Title": "Dubai",
            "TagLine": "Dubai, United Arab Emirates",
            "Lat": "25.210751",
            "Lon": "55.314140",
            "Identifier": "133479" 
        },
        "CheckIn": (datetime.datetime.now() + datetime.timedelta(days=180)).strftime("%Y-%m-%d"),
        "CheckOut": (datetime.datetime.now() + datetime.timedelta(days=181)).strftime("%Y-%m-%d"),
        "GuestNationality": "PK",
        "Rooms": [{"Adults": 2, "Children": []}],
        "RoomsCount": 1,
        "AdultsCount": 2,
        "ChildrenCount": 0
    }
    
    res = session.post(search_url, headers=headers, json=payload)
    if res.status_code != 200:
        print("Search failed:", res.text)
        return []
        
    data = res.json()
    search_ref_id = data.get("RefID")
    raw_itineraries = data.get("Itineraries", [])
    
    # 3. Poll for more results to ensure we have a good batch
    poll_count = 0
    while data.get("Poll") and search_ref_id and poll_count < 3:
        print(f"Polling Bookme API (Poll {poll_count+1})...")
        time.sleep(3)
        poll_payload = payload.copy()
        poll_payload["RefID"] = search_ref_id
        try:
            poll_res = session.post(search_url, headers=headers, json=poll_payload)
            if poll_res.status_code == 200:
                poll_data = poll_res.json()
                new_its = poll_data.get("Itineraries", [])
                if not new_its:
                    break
                raw_itineraries.extend(new_its)
                print(f"Accumulated {len(raw_itineraries)} hotels so far.")
            else:
                break
        except Exception as e:
            print(f"Polling network error: {e}. Retrying in 10s...")
            time.sleep(10)
            continue
        poll_count += 1
        
    print(f"Finished polling Bookme. Total raw hotels: {len(raw_itineraries)}")
    
    # We yield raw hotels for lazy loading rooms
    hotels_to_process = []
    for it in raw_itineraries:
        prop = it.get("Property", {})
        if prop.get("Name"):
            hotels_to_process.append({
                "name": prop.get("Name"),
                "address": prop.get("Address", {}).get("Info", ""),
                "itinerary_ref_id": it.get("RefID"),
                "search_ref_id": search_ref_id,
                "session": session, # Pass session so we can fetch rooms later
                "headers": headers
            })
            
    # Also save the raw intermediate file, as user noticed it existed
    with open("bookme_hotels_raw_search.json", "w") as f:
        json.dump(data, f, indent=2)
        
    return hotels_to_process

def slugify(text):
    text = text.lower().replace('&', 'and')
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s-]+', '-', text).strip('-')
    return text

def get_agoda_slug_url(hotel_name):
    slug = slugify(hotel_name)
    url = f"https://www.agoda.com/en-us/{slug}/hotel/dubai-ae.html"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        res = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
        # If agoda doesn't find the hotel, it redirects to the city search page or home page
        if "/hotel/" in res.url:
            return res.url
    except Exception as e:
        print(f"Agoda Slug Error: {e}")
    return None

def fetch_bookme_rooms(hotel):
    """Fetches rooms for a single Bookme hotel dynamically."""
    single_url = f"https://api.bookmesky.com/hotels/api/single-itinerary?RefID={hotel['search_ref_id']}&ItineraryRefID={hotel['itinerary_ref_id']}"
    room_names = []
    try:
        r = hotel["session"].get(single_url, headers=hotel["headers"])
        if r.status_code == 200:
            rooms_data = r.json()
            options = rooms_data.get("Itinerary", {}).get("Options", [])
            room_names = list(set([opt.get("Title") for opt in options if opt.get("Title")]))
    except Exception as e:
        print(f"Error fetching rooms for {hotel['name']}: {e}")
        
    return room_names

async def cross_reference_agoda(bookme_hotels, target_count=50):
    print(f"\nStarting Agoda Cross-referencing. Target: {target_count} matches.")
    results = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 800}
        )
        
        for hotel in bookme_hotels:
            if len(results) >= target_count:
                print(f"\nSuccessfully reached target of {target_count} matched hotels!")
                break
                
            hotel_name = hotel['name']
            print(f"\nChecking Agoda for: {hotel_name}")
            
            # 1. MATCH FIRST using Slug Routing
            hotel_url = get_agoda_slug_url(hotel_name)
            if not hotel_url:
                print(f"  -> No Agoda match found via Slug Routing for '{hotel_name}'. Skipping.")
                continue
                
            print(f"  -> Match found! URL: {hotel_url}...")
            
            # Append dates to prevent Date Picker modal overlay
            if "?" not in hotel_url:
                hotel_url += "?checkIn=2026-08-01&checkOut=2026-08-02&los=1&rooms=1&adults=2"
            
            # 2. Extract Agoda Rooms
            page = await context.new_page()
            agoda_rooms = []
            agoda_address = ""
            try:
                await page.goto(hotel_url, wait_until="domcontentloaded", timeout=60000)
                
                # Scroll incrementally to trigger lazy loading of the room grid
                rooms_found = False
                for i in range(15):
                    await page.evaluate("window.scrollBy(0, 700)")
                    await page.wait_for_timeout(1200)
                    if not rooms_found and await page.locator("div[id^='room-item-']").count() > 0:
                        rooms_found = True
                        # Don't break immediately! Keep scrolling to load the rest of the rooms
                        
                # Wait for rooms grid to appear if not already there
                if not rooms_found:
                    try:
                        await page.wait_for_selector("div[id^='room-item-']", timeout=5000)
                    except Exception:
                        print("  -> Timeout waiting for rooms grid (might be sold out or lazy loaded).")
                
                addr_locator = page.locator("span[data-selenium='hotel-address-map']")
                if await addr_locator.count() > 0:
                    agoda_address = await addr_locator.first.inner_text()
                    
                room_containers = await page.locator("div[id^='room-item-']").all()
                if not room_containers:
                    print("  -> Match found but no rooms loaded on Agoda.")
                    await page.close()
                    continue
                    
                for room in room_containers:
                    name_locator = room.locator("h4, h3")
                    if await name_locator.count() > 0:
                        room_name = await name_locator.first.inner_text()
                        
                        # Get all images available in the container
                        img_locator = room.locator("img")
                        img_urls = []
                        for idx in range(await img_locator.count()):
                            src = await img_locator.nth(idx).get_attribute("src")
                            if src and not src.endswith('.svg') and src not in img_urls:
                                img_urls.append(src)
                                
                        if room_name.strip() and not any(r['room_name'] == room_name.strip() for r in agoda_rooms):
                            agoda_rooms.append({
                                "room_name": room_name.strip(),
                                "image_urls": img_urls
                            })
            except Exception as e:
                print(f"  -> Error scraping Agoda for {hotel_name}: {e}")
            finally:
                await page.close()
                
            if not agoda_rooms:
                print(f"  -> Match found but no rooms loaded on Agoda.")
                continue
                
            # 3. IF AGODA HAS ROOMS, NOW WE FETCH BOOKME ROOMS (Economic approach)
            print(f"  -> Scraping Bookme rooms (since Agoda matched!)...")
            bookme_rooms = fetch_bookme_rooms(hotel)
            
            # Combine Bookme and Agoda Data
            combined_data = {
                "hotel_name": hotel_name,
                "address_bookme": hotel["address"],
                "address_agoda": agoda_address.strip(),
                "rooms_bookme": bookme_rooms,
                "rooms_agoda": agoda_rooms
            }
            results.append(combined_data)
            print(f"  -> Successfully scraped BOTH! ({len(results)}/{target_count})")
            
            # Progressively save
            with open("dubai_hotels_matched.json", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=4, ensure_ascii=False)
                
        await browser.close()
        
    return results

def main():
    print("========================================")
    print(" BOOKME & AGODA CROSS-REFERENCE SCRAPER ")
    print("========================================")
    
    # 1. Fetch Bookme Data
    bookme_list = get_bookme_hotels_dubai()
    if not bookme_list:
        print("Failed to fetch Bookme hotels. Exiting.")
        return
        
    # 2. Fetch Agoda Data and Cross-reference
    asyncio.run(cross_reference_agoda(bookme_list, target_count=50))
    
    print("\nExtraction complete! Data saved to dubai_hotels_matched.json")

if __name__ == "__main__":
    main()
