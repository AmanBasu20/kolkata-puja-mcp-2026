"""
Kolkata Puja Tourist MCP - AI-Assisted Enrichment Pipeline (Resume Edition)

Features Smart Resume: Preserves existing data from pandals 1-98.
Uses Llama-3.3-70b to bypass previous model rate limits.
"""

import json
import time
import os
import sys
import requests
from pathlib import Path
from datetime import datetime, timezone
from groq import Groq

# Configure API Keys
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "126bd074c0501a736a2fc0fb60eabac9097ef6bd") 

client = Groq(api_key=GROQ_API_KEY)
MODEL_NAME = "openai/gpt-oss-120b"

# RESUME INDEX
START_INDEX = 171

BASE_DIR = Path(__file__).resolve().parent.parent
PANDALS_FILE = BASE_DIR / "data" / "static" / "pandals_2026.json"
THEMES_FILE = BASE_DIR / "data" / "dynamic" / "pandal_themes_2026.json"

def fetch_serper(query, retries=3):
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "gl": "in", "num": 3})
    headers = {'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'}
    
    for attempt in range(retries):
        try:
            response = requests.post(url, headers=headers, data=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            results = [{"snippet": item.get("snippet", ""), "url": item.get("link", "")} for item in data.get("organic", [])]
            return results
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                print(f"      [!] Serper API failed for '{query}': {e}")
                return []
            time.sleep(2 ** attempt)

def extract_with_llm(pandal_name, search_context, retries=3):
    prompt = f"""You are an exact data extraction agent for Kolkata Durga Puja.
Analyze these search results for the pandal: "{pandal_name}".

RULES:
1. Only extract a field when the source explicitly associates it with EXACTLY "{pandal_name}".
2. Do NOT infer from geographic proximity.
3. Do NOT infer from another pandal mentioned in the same article.
4. Do NOT use 2025 or older information. Must be strictly 2026.
5. Do NOT use predictions or "expected" themes.

If a field is missing, ambiguous, or violates the rules, output null for the value.

Return EXACTLY a valid JSON object matching this structure:
{{
    "theme": "string or null",
    "artist": "string or null",
    "idol_maker": "string or null",
    "description": "A 1-sentence summary based ONLY on the text, or null if no info exists"
}}

SEARCH RESULTS TO ANALYZE:
{json.dumps(search_context, indent=2)}
"""
    
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": "You output only raw, valid JSON. Do not use markdown backticks."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0
            )
            
            raw_output = response.choices[0].message.content
            clean_json = raw_output.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_json)
            
        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "rate limit" in error_str:
                print(f"\n      [🚨] FATAL RATE LIMIT HIT: {e}")
                print("      [🚨] Saving current progress and shutting down to prevent data loss...")
                return "RATE_LIMIT_ERROR"
            
            if attempt == retries - 1:
                print(f"      [!] Groq LLM Extraction failed: {e}")
                return None
            time.sleep(2 ** attempt)

def get_empty_schema():
    return {"theme": None, "artist": None, "idol_maker": None, "description": None}

def save_to_disk(themes_dict, now_utc):
    final_output = {
        "dataset_name": "Kolkata Puja 2026 Themes - AI Extracted",
        "last_collected_at": now_utc,
        "themes_by_pandal": themes_dict
    }
    THEMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(THEMES_FILE, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=2, ensure_ascii=False)

def build_themes():
    if not PANDALS_FILE.exists():
        print(f"[!] {PANDALS_FILE} not found.")
        return

    with open(PANDALS_FILE, "r", encoding="utf-8") as f:
        pandals = json.load(f)

    now_utc = datetime.now(timezone.utc).isoformat()
    
    # Load existing data to preserve progress (Pandals 1 through 98)
    themes_dict = {}
    if THEMES_FILE.exists():
        try:
            with open(THEMES_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
                themes_dict = existing_data.get("themes_by_pandal", {})
                print(f"[ℹ️] Loaded {len(themes_dict)} existing pandal records from disk.")
        except Exception as e:
            print(f"[!] Could not load existing themes: {e}")

    print(f"\nStarting Pipeline. Resuming from Pandal #{START_INDEX} using {MODEL_NAME}...\n")

    for idx, pdl in enumerate(pandals, 1):
        if idx < START_INDEX:
            # We skip scraping, but the data is already safely inside themes_dict
            continue
            
        p_id = pdl["id"]
        p_name = pdl["name"]
        
        print(f"[{idx}/{len(pandals)}] 🤖 Processing: {p_name}...")
        
        context = {
            "theme_results": fetch_serper(f'"{p_name}" 2026 theme'),
            "artist_results": fetch_serper(f'"{p_name}" 2026 artist'),
            "idol_results": fetch_serper(f'"{p_name}" 2026 idol OR "idol artist"')
        }
        
        has_content = any(len(res) > 0 for res in context.values())
        
        if has_content:
            extracted_data = extract_with_llm(p_name, context)
            
            if extracted_data == "RATE_LIMIT_ERROR":
                save_to_disk(themes_dict, now_utc)
                sys.exit(1) # Gracefully exit the entire script immediately
                
            if extracted_data:
                themes_dict[p_id] = {
                    "pandal_id": p_id,
                    "pandal_name": p_name,
                    "theme": extracted_data.get("theme"),
                    "artist": extracted_data.get("artist"),
                    "idol_maker": extracted_data.get("idol_maker"),
                    "description": extracted_data.get("description")
                }
                
                if extracted_data.get("theme"):
                    print(f"      [+] Verified Theme: {extracted_data.get('theme')}")
                else:
                    print("      [-] No verified theme found.")
            else:
                print("      [-] LLM extraction failed. Applying null schema.")
                themes_dict[p_id] = {"pandal_id": p_id, "pandal_name": p_name, **get_empty_schema()}
        else:
            print("      [-] No web data found. Applying null schema.")
            themes_dict[p_id] = {"pandal_id": p_id, "pandal_name": p_name, **get_empty_schema()}

        # Save checkpoint
        if idx % 5 == 0:
            save_to_disk(themes_dict, now_utc)
            print("      [💾] Checkpoint saved.")

    save_to_disk(themes_dict, now_utc)
    print(f"\n✅ SUCCESS: Enrichment pipeline complete!")

if __name__ == "__main__":
    build_themes()