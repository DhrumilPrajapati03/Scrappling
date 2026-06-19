import streamlit as st
import pandas as pd
import json
import os
import re
import requests
import datetime
import uuid
from dotenv import load_dotenv
from urllib.parse import urlparse

# Load environment variables from .env file
load_dotenv()

# Suppress SSL warnings for website scraping (many school sites have cert issues)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Page Configuration ---
st.set_page_config(
    page_title="B2B Lead Finder & Email Extractor",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Files Config ---
SCRAPES_FILE = "scrapes.json"

# --- Load / Save Scrapes History Helpers ---
def load_scrapes():
    if os.path.exists(SCRAPES_FILE):
        try:
            with open(SCRAPES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_scrape_session(session_data):
    history = load_scrapes()
    # Add new session to the top
    history.insert(0, session_data)
    with open(SCRAPES_FILE, "w") as f:
        json.dump(history, f, indent=4)

# --- Outscraper API Client ---
# --- Website Email Scraper (fallback when API enrichment doesn't return emails) ---
def scrape_emails_from_website(url, timeout=10):
    """Crawl a website URL and extract email addresses from the page HTML.
    Looks for mailto: links and email patterns in the page content.
    Returns a list of unique, cleaned email addresses."""
    if not url or url == "N/A":
        return []
    
    # Normalize URL
    if not url.startswith("http"):
        url = "https://" + url
    
    emails_found = set()
    
    # Pages to check: homepage + common contact pages
    pages_to_check = [url]
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    for path in ["/contact", "/contact-us", "/contactus", "/about", "/about-us"]:
        pages_to_check.append(base_url + path)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    # Email regex pattern - matches standard email formats
    email_pattern = re.compile(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
        re.IGNORECASE
    )
    
    # Exclusion patterns for false positives (image files, css, js, etc.)
    exclude_extensions = {
        '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico',
        '.css', '.js', '.woff', '.woff2', '.ttf', '.eot', '.map'
    }
    
    for page_url in pages_to_check:
        try:
            resp = requests.get(page_url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            if resp.status_code == 200:
                text = resp.text
                
                # Find all email patterns
                found = email_pattern.findall(text)
                for email in found:
                    email_lower = email.lower().strip()
                    # Filter out false positives
                    _, ext = os.path.splitext(email_lower)
                    if ext in exclude_extensions:
                        continue
                    # Skip common non-email patterns
                    if email_lower.endswith('.html') or email_lower.endswith('.php'):
                        continue
                    if 'example.com' in email_lower or 'sentry.io' in email_lower:
                        continue
                    if 'wixpress.com' in email_lower or 'schema.org' in email_lower:
                        continue
                    emails_found.add(email_lower)
        except Exception:
            continue
    
    return list(emails_found)


def extract_emails_from_lead(lead):
    """Extract all email addresses from a lead dict, checking all possible field names
    that Outscraper might return (email_1, email_2, email_3, email, 
    emails_and_contacts nested fields, etc.)."""
    emails = []
    
    # Check direct email fields (email_1, email_2, email_3, email)
    for key in ["email_1", "email_2", "email_3", "email"]:
        val = lead.get(key)
        if val and isinstance(val, str) and val.strip() and val not in emails:
            emails.append(val.strip())
    
    # Check enrichment dicts (Outscraper nests data under 'contacts_n_leads' or legacy 'emails_and_contacts')
    for enrichment_key in ["contacts_n_leads", "emails_and_contacts"]:
        enrichment = lead.get(enrichment_key)
        if isinstance(enrichment, dict):
            for key in ["email_1", "email_2", "email_3", "email"]:
                val = enrichment.get(key)
                if val and isinstance(val, str) and val.strip() and val not in emails:
                    emails.append(val.strip())
            # Also check if there's an 'emails' list
            email_list = enrichment.get("emails", [])
            if isinstance(email_list, list):
                for val in email_list:
                    if val and isinstance(val, str) and val.strip() and val not in emails:
                        emails.append(val.strip())
    
    # Check if there's a top-level 'emails' list
    email_list = lead.get("emails", [])
    if isinstance(email_list, list):
        for val in email_list:
            if val and isinstance(val, str) and val.strip() and val not in emails:
                emails.append(val.strip())
    
    return emails


def run_live_scrape(api_key, category, location, limit):
    from outscraper import ApiClient
    
    client = ApiClient(api_key=api_key)
    
    query_str = f"{category} in {location}"
    
    # Use enrichment to crawl websites for emails & social profiles
    # Note: The correct enrichment name is 'contacts_n_leads' (not 'emails_and_contacts')
    results = client.google_maps_search(
        query_str,
        limit=limit,
        enrichment=["contacts_n_leads"]
    )
    
    # Outscraper SDK returns nested lists: [[{...}, {...}]] (one list per query)
    flat_results = []
    if results:
        if isinstance(results[0], list):
            for query_results in results:
                flat_results.extend(query_results)
        else:
            flat_results.extend(results)
    
    # --- Fallback: scrape emails from websites for leads missing email data ---
    for lead in flat_results:
        existing_emails = extract_emails_from_lead(lead)
        if not existing_emails:
            # No email found from API enrichment, try scraping the website directly
            website_url = lead.get("site") or lead.get("website") or ""
            if website_url:
                scraped_emails = scrape_emails_from_website(website_url)
                # Inject scraped emails back into the lead dict
                for i, email in enumerate(scraped_emails[:3]):  # Cap at 3 emails
                    lead[f"email_{i+1}"] = email
            
    return flat_results

# --- Mock Data for Demo Mode (Ahmedabad Region) ---
MOCK_LEADS = [
    {
        "name": "Nirma University",
        "category": "University",
        "type": "University",
        "phone": "+91 2717 241900",
        "site": "https://nirmauni.ac.in",
        "postal_code": "382481",
        "full_address": "Sarkhej - Gandhinagar Highway, Gota, Ahmedabad, Gujarat 382481, India",
        "latitude": 23.1287,
        "longitude": 72.5442,
        "rating": 4.6,
        "reviews": 2450,
        "email_1": "admissions@nirmauni.ac.in",
        "email_2": "info@nirmauni.ac.in",
        "facebook": "https://facebook.com/nirmauni",
        "linkedin": "https://linkedin.com/school/nirma-university",
        "instagram": "https://instagram.com/nirma.university",
        "twitter": "https://twitter.com/NirmaUni",
        "working_hours": {"Monday": "9:00 AM - 5:00 PM", "Tuesday": "9:00 AM - 5:00 PM", "Wednesday": "9:00 AM - 5:00 PM", "Thursday": "9:00 AM - 5:00 PM", "Friday": "9:00 AM - 5:00 PM", "Saturday": "9:00 AM - 1:00 PM"}
    },
    {
        "name": "Ahmedabad Institute of Technology (AIT)",
        "category": "College",
        "type": "Engineering College",
        "phone": "+91 79 2685 1234",
        "site": "http://www.aitindia.in",
        "postal_code": "382481",
        "full_address": "Gota-Ognaj Road, Ognaj, Ahmedabad, Gujarat 382481, India",
        "latitude": 23.1118,
        "longitude": 72.5029,
        "rating": 4.0,
        "reviews": 680,
        "email_1": "info@aitindia.in",
        "email_2": "admission@aitindia.in",
        "facebook": "https://facebook.com/aitahmedabad",
        "linkedin": "https://linkedin.com/school/ahmedabad-institute-of-technology",
        "instagram": "https://instagram.com/ait_ahmedabad",
        "twitter": "",
        "working_hours": {"Monday": "8:30 AM - 4:30 PM", "Tuesday": "8:30 AM - 4:30 PM", "Wednesday": "8:30 AM - 4:30 PM", "Thursday": "8:30 AM - 4:30 PM", "Friday": "8:30 AM - 4:30 PM", "Saturday": "Closed"}
    },
    {
        "name": "L.D. College of Engineering",
        "category": "College",
        "type": "Engineering College",
        "phone": "+91 79 2630 2887",
        "site": "https://ldce.ac.in",
        "postal_code": "380015",
        "full_address": "Opposite Gujarat University, Navrangpura, Ahmedabad, Gujarat 380015, India",
        "latitude": 23.0336,
        "longitude": 72.5467,
        "rating": 4.5,
        "reviews": 3200,
        "email_1": "principal@ldce.ac.in",
        "email_2": "ldce_dah@yahoo.com",
        "facebook": "https://facebook.com/ldceahmedabad",
        "linkedin": "https://linkedin.com/school/l-d-college-of-engineering",
        "instagram": "https://instagram.com/ldce.official",
        "twitter": "https://twitter.com/ldceahmedabad",
        "working_hours": {"Monday": "10:30 AM - 6:10 PM", "Tuesday": "10:30 AM - 6:10 PM", "Wednesday": "10:30 AM - 6:10 PM", "Thursday": "10:30 AM - 6:10 PM", "Friday": "10:30 AM - 6:10 PM", "Saturday": "Closed"}
    },
    {
        "name": "St. Xavier's College",
        "category": "College",
        "type": "Arts and Science College",
        "phone": "+91 79 2630 8057",
        "site": "https://sxca.edu.in",
        "postal_code": "380009",
        "full_address": "Netaji Road, Navrangpura, Ahmedabad, Gujarat 380009, India",
        "latitude": 23.0366,
        "longitude": 72.5539,
        "rating": 4.6,
        "reviews": 1450,
        "email_1": "info@sxca.edu.in",
        "email_2": "admissions@sxca.edu.in",
        "facebook": "https://facebook.com/stxavierscollegeahmedabad",
        "linkedin": "https://linkedin.com/school/st-xavier's-college-autonomous-ahmedabad",
        "instagram": "https://instagram.com/stxavierscollegeahmedabad",
        "twitter": "",
        "working_hours": {"Monday": "8:00 AM - 4:00 PM", "Tuesday": "8:00 AM - 4:00 PM", "Wednesday": "8:00 AM - 4:00 PM", "Thursday": "8:00 AM - 4:00 PM", "Friday": "8:00 AM - 4:00 PM", "Saturday": "8:00 AM - 12:30 PM"}
    },
    {
        "name": "St. Xavier's High School Loyola Hall",
        "category": "School",
        "type": "High School",
        "phone": "+91 79 2791 2288",
        "site": "http://loyolahall.org",
        "postal_code": "380013",
        "full_address": "Loyola Hall, Memnagar, Ahmedabad, Gujarat 380013, India",
        "latitude": 23.0475,
        "longitude": 72.5358,
        "rating": 4.7,
        "reviews": 950,
        "email_1": "info@loyolahall.org",
        "email_2": "principal@loyolahall.org",
        "facebook": "https://facebook.com/loyolahallahmedabad",
        "linkedin": "",
        "instagram": "https://instagram.com/loyola_hall",
        "twitter": "",
        "working_hours": {"Monday": "7:30 AM - 1:30 PM", "Tuesday": "7:30 AM - 1:30 PM", "Wednesday": "7:30 AM - 1:30 PM", "Thursday": "7:30 AM - 1:30 PM", "Friday": "7:30 AM - 1:30 PM", "Saturday": "7:30 AM - 11:30 AM"}
    },
    {
        "name": "Delhi Public School (DPS) Bopal",
        "category": "School",
        "type": "High School",
        "phone": "+91 2717 230521",
        "site": "https://dpsbopal-ahmedabad.edu.in",
        "postal_code": "380058",
        "full_address": "Bopal-Ghuma Road, Bopal, Ahmedabad, Gujarat 380058, India",
        "latitude": 23.0232,
        "longitude": 72.4589,
        "rating": 4.5,
        "reviews": 1100,
        "email_1": "dpsbopal@kalorex.org",
        "email_2": "admission@dpsbopal.edu.in",
        "facebook": "https://facebook.com/dpsbopalahmedabad",
        "linkedin": "",
        "instagram": "https://instagram.com/dpsbopal",
        "twitter": "https://twitter.com/dpsbopal",
        "working_hours": {"Monday": "8:00 AM - 2:00 PM", "Tuesday": "8:00 AM - 2:00 PM", "Wednesday": "8:00 AM - 2:00 PM", "Thursday": "8:00 AM - 2:00 PM", "Friday": "8:00 AM - 2:00 PM", "Saturday": "Closed"}
    },
    {
        "name": "Udgam School for Children",
        "category": "School",
        "type": "Primary & High School",
        "phone": "+91 79 7101 2222",
        "site": "https://www.udgamschool.com",
        "postal_code": "380054",
        "full_address": "Opp. Drive-in Cinema, Thaltej, Ahmedabad, Gujarat 380054, India",
        "latitude": 23.0489,
        "longitude": 72.5186,
        "rating": 4.4,
        "reviews": 1350,
        "email_1": "info@udgamschool.com",
        "email_2": "careers@udgamschool.com",
        "facebook": "https://facebook.com/udgamschool",
        "linkedin": "https://linkedin.com/company/udgamschool",
        "instagram": "https://instagram.com/udgamschool",
        "twitter": "",
        "working_hours": {"Monday": "7:30 AM - 2:30 PM", "Tuesday": "7:30 AM - 2:30 PM", "Wednesday": "7:30 AM - 2:30 PM", "Thursday": "7:30 AM - 2:30 PM", "Friday": "7:30 AM - 2:30 PM", "Saturday": "Closed"}
    },
    {
        "name": "Vishwa Computer Education",
        "category": "Computer Training School",
        "type": "Computer classes",
        "phone": "+91 98250 12345",
        "site": "http://www.vishwacomputer.com",
        "postal_code": "380009",
        "full_address": "102, Ashram Road, Near C.G. Road, Navrangpura, Ahmedabad, Gujarat 380009, India",
        "latitude": 23.0315,
        "longitude": 72.5621,
        "rating": 4.8,
        "reviews": 320,
        "email_1": "contact@vishwacomputer.com",
        "email_2": "enquiry@vishwacomputer.com",
        "facebook": "https://facebook.com/vishwacomputer",
        "linkedin": "",
        "instagram": "",
        "twitter": "",
        "working_hours": {"Monday": "8:00 AM - 8:00 PM", "Tuesday": "8:00 AM - 8:00 PM", "Wednesday": "8:00 AM - 8:00 PM", "Thursday": "8:00 AM - 8:00 PM", "Friday": "8:00 AM - 8:00 PM", "Saturday": "8:00 AM - 6:00 PM"}
    },
    {
        "name": "Red & White Multimedia Education",
        "category": "Computer Training School",
        "type": "Vocational institute",
        "phone": "+91 95123 40001",
        "site": "https://www.rnwmultimedia.edu.in",
        "postal_code": "380015",
        "full_address": "Royal Arcade, Near Dev Arc Mall, Satellite, Ahmedabad, Gujarat 380015, India",
        "latitude": 23.0256,
        "longitude": 72.5124,
        "rating": 4.7,
        "reviews": 890,
        "email_1": "info@rnwmultimedia.edu.in",
        "email_2": "satellite@rnwmultimedia.edu.in",
        "facebook": "https://facebook.com/rnwmultimedia",
        "linkedin": "https://linkedin.com/company/red-&-white-multimedia-education",
        "instagram": "https://instagram.com/rnw_multimedia",
        "twitter": "https://twitter.com/rnwmultimedia",
        "working_hours": {"Monday": "8:00 AM - 9:00 PM", "Tuesday": "8:00 AM - 9:00 PM", "Wednesday": "8:00 AM - 9:00 PM", "Thursday": "8:00 AM - 9:00 PM", "Friday": "8:00 AM - 9:00 PM", "Saturday": "8:00 AM - 7:00 PM"}
    },
    {
        "name": "Allen Career Institute Ahmedabad",
        "category": "Coaching Centre",
        "type": "Entrance exam classes",
        "phone": "+91 79 4903 3100",
        "site": "https://www.allen.ac.in",
        "postal_code": "380054",
        "full_address": "S.G. Highway, Thaltej, Ahmedabad, Gujarat 380054, India",
        "latitude": 23.0543,
        "longitude": 72.5111,
        "rating": 4.3,
        "reviews": 1540,
        "email_1": "infoadi@allen.ac.in",
        "email_2": "ahmedabad@allen.ac.in",
        "facebook": "https://facebook.com/allenahmedabad",
        "linkedin": "",
        "instagram": "https://instagram.com/allen_ahmedabad",
        "twitter": "",
        "working_hours": {"Monday": "8:00 AM - 8:00 PM", "Tuesday": "8:00 AM - 8:00 PM", "Wednesday": "8:00 AM - 8:00 PM", "Thursday": "8:00 AM - 8:00 PM", "Friday": "8:00 AM - 8:00 PM", "Saturday": "8:00 AM - 8:00 PM"}
    },
    {
        "name": "Aakash Institute Drive-In",
        "category": "Coaching Centre",
        "type": "Tuition classes",
        "phone": "+91 79 4800 8899",
        "site": "https://www.aakash.ac.in",
        "postal_code": "380054",
        "full_address": "First Floor, Drive-In Road, Thaltej, Ahmedabad, Gujarat 380054, India",
        "latitude": 23.0461,
        "longitude": 72.5273,
        "rating": 4.2,
        "reviews": 980,
        "email_1": "medical@aesl.in",
        "email_2": "iitjee@aesl.in",
        "facebook": "https://facebook.com/aakashdrivein",
        "linkedin": "",
        "instagram": "",
        "twitter": "",
        "working_hours": {"Monday": "9:00 AM - 7:30 PM", "Tuesday": "9:00 AM - 7:30 PM", "Wednesday": "9:00 AM - 7:30 PM", "Thursday": "9:00 AM - 7:30 PM", "Friday": "9:00 AM - 7:30 PM", "Saturday": "9:00 AM - 6:00 PM"}
    },
    {
        "name": "Mount Carmel High School",
        "category": "School",
        "type": "Girls School",
        "phone": "+91 79 2658 9700",
        "site": "https://mountcarmelahmedabad.org",
        "postal_code": "380009",
        "full_address": "Ashram Road, Navrangpura, Ahmedabad, Gujarat 380009, India",
        "latitude": 23.0311,
        "longitude": 72.5712,
        "rating": 4.4,
        "reviews": 420,
        "email_1": "mountcarmel_ahmd@yahoo.co.in",
        "email_2": "",
        "facebook": "",
        "linkedin": "",
        "instagram": "",
        "twitter": "",
        "working_hours": {"Monday": "7:45 AM - 1:45 PM", "Tuesday": "7:45 AM - 1:45 PM", "Wednesday": "7:45 AM - 1:45 PM", "Thursday": "7:45 AM - 1:45 PM", "Friday": "7:45 AM - 1:45 PM", "Saturday": "7:45 AM - 11:30 AM"}
    },
    {
        "name": "H.L. College of Commerce",
        "category": "College",
        "type": "Commerce College",
        "phone": "+91 79 2646 2820",
        "site": "https://www.hlcollege.edu",
        "postal_code": "380009",
        "full_address": "HL Campus, Navrangpura, Ahmedabad, Gujarat 380009, India",
        "latitude": 23.0381,
        "longitude": 72.5489,
        "rating": 4.3,
        "reviews": 720,
        "email_1": "mail@hlcollege.edu",
        "email_2": "admissions@hlcollege.edu",
        "facebook": "https://facebook.com/hlccahmedabad",
        "linkedin": "https://linkedin.com/school/h-l-college-of-commerce",
        "instagram": "",
        "twitter": "",
        "working_hours": {"Monday": "7:30 AM - 1:30 PM", "Tuesday": "7:30 AM - 1:30 PM", "Wednesday": "7:30 AM - 1:30 PM", "Thursday": "7:30 AM - 1:30 PM", "Friday": "7:30 AM - 1:30 PM", "Saturday": "7:30 AM - 11:30 AM"}
    },
    {
        "name": "Shanti Asiatic School",
        "category": "School",
        "type": "High School",
        "phone": "+91 90990 79809",
        "site": "http://shantiasiatic.com",
        "postal_code": "380058",
        "full_address": "Opp. Vraj Gardens, Off S.P. Ring Road, Shela, Ahmedabad, Gujarat 380058, India",
        "latitude": 23.0019,
        "longitude": 72.4485,
        "rating": 4.4,
        "reviews": 310,
        "email_1": "info@shantiasiatic.com",
        "email_2": "admissions@shantiasiatic.com",
        "facebook": "https://facebook.com/shantiasiaticschool",
        "linkedin": "",
        "instagram": "https://instagram.com/shantiasiatic",
        "twitter": "",
        "working_hours": {"Monday": "8:30 AM - 3:00 PM", "Tuesday": "8:30 AM - 3:00 PM", "Wednesday": "8:30 AM - 3:00 PM", "Thursday": "8:30 AM - 3:00 PM", "Friday": "8:30 AM - 3:00 PM", "Saturday": "8:30 AM - 12:30 PM"}
    },
    {
        "name": "Silver Oak University (SOU)",
        "category": "University",
        "type": "University",
        "phone": "+91 79 6604 6300",
        "site": "https://silveroak.uni.edu.in",
        "postal_code": "382481",
        "full_address": "Near Bhavik Publication, Opp. Bhagwat Vidyapith, Gota, Ahmedabad, Gujarat 382481, India",
        "latitude": 23.0975,
        "longitude": 72.5312,
        "rating": 4.2,
        "reviews": 1980,
        "email_1": "info@silveroak.uni.edu.in",
        "email_2": "admission@silveroak.uni.edu.in",
        "facebook": "https://facebook.com/silveroakuni",
        "linkedin": "https://linkedin.com/school/silver-oak-university",
        "instagram": "https://instagram.com/silver_oak_university",
        "twitter": "https://twitter.com/silveroakuni",
        "working_hours": {"Monday": "9:00 AM - 5:00 PM", "Tuesday": "9:00 AM - 5:00 PM", "Wednesday": "9:00 AM - 5:00 PM", "Thursday": "9:00 AM - 5:00 PM", "Friday": "9:00 AM - 5:00 PM", "Saturday": "9:00 AM - 2:00 PM"}
    }
]

def get_mock_scrape(category, limit):
    # Filter by category matching
    cat_lower = category.lower()
    
    # Simple semantic matching
    filtered = []
    for lead in MOCK_LEADS:
        lead_cat = lead["category"].lower()
        lead_type = lead["type"].lower()
        
        match = False
        if "school" in cat_lower and ("school" in lead_cat or "school" in lead_type):
            match = True
        elif "college" in cat_lower and ("college" in lead_cat or "college" in lead_type or "university" in lead_cat):
            match = True
        elif "university" in cat_lower and ("university" in lead_cat or "university" in lead_type):
            match = True
        elif ("computer" in cat_lower or "classes" in cat_lower or "training" in cat_lower) and \
             ("computer" in lead_cat or "coaching" in lead_cat or "training" in lead_cat):
            match = True
        elif "coaching" in cat_lower and ("coaching" in lead_cat or "tuition" in lead_type):
            match = True
        elif cat_lower == "educational institution" or cat_lower == "custom":
            match = True # matches all
            
        if match:
            filtered.append(lead)
            
    # If no match, just return everything
    if not filtered:
        filtered = MOCK_LEADS
        
    return filtered[:limit]

# --- Main App ---
def main():
    # Load API key: try Streamlit secrets first (for cloud deployment), then .env
    try:
        api_key = st.secrets["OUTSCRAPER_API_KEY"]
    except (KeyError, FileNotFoundError):
        api_key = os.environ.get("OUTSCRAPER_API_KEY", "")
    
    # Custom CSS for modern dashboard style
    st.markdown("""
        <style>
        .main-header {
            font-size: 2.2rem;
            color: #1E3A8A;
            font-weight: 700;
            margin-bottom: 0.2rem;
        }
        .subheader {
            color: #4B5563;
            margin-bottom: 1.5rem;
            font-size: 1.05rem;
        }
        .card {
            background-color: #F8FAFC;
            padding: 1.2rem;
            border-radius: 8px;
            border: 1px solid #E2E8F0;
            margin-bottom: 1rem;
        }
        .metric-title {
            font-size: 0.85rem;
            color: #64748B;
            font-weight: 600;
        }
        .metric-value {
            font-size: 1.5rem;
            color: #0F172A;
            font-weight: 700;
        }
        /* Custom styles for tables/lists */
        .email-pill {
            background-color: #DBEAFE;
            color: #1E40AF;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: 600;
        }
        .status-pill {
            background-color: #F1F5F9;
            color: #475569;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: 600;
        }
        </style>
    """, unsafe_allow_html=True)

    # --- Header ---
    st.markdown("<div class='main-header'>Lead Finder Dashboard</div>", unsafe_allow_html=True)
    st.markdown("<div class='subheader'>Search for B2B educational leads in Ahmedabad and generate direct academy proposals.</div>", unsafe_allow_html=True)

    # --- Sidebar Configuration ---
    st.sidebar.title("Configuration ⚙️")
    
    # Display API key status
    if api_key:
        masked_key = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "****"
        st.sidebar.success(f"🔑 API Key loaded successfully")
        st.sidebar.caption(f"Key: `{masked_key}`")
    else:
        st.sidebar.warning("⚠️ No `OUTSCRAPER_API_KEY` found.")
        st.sidebar.caption(
            "**Local:** Add to `.env` file:\n`OUTSCRAPER_API_KEY=your_key_here`\n\n"
            "**Streamlit Cloud:** Add to app Settings → Secrets:\n`OUTSCRAPER_API_KEY = \"your_key_here\"`"
        )
        
    # Demo Mode switch
    demo_mode = st.sidebar.toggle("Run in Demo Mode", value=(not api_key))
    
    if demo_mode:
        st.sidebar.info("🧪 Running in **Demo Mode**. Using high-quality simulated educational leads in Ahmedabad.")
    elif not api_key:
        st.sidebar.warning("⚠️ No API key in `.env`. Switched to **Demo Mode** automatically.")
        demo_mode = True
    else:
        st.sidebar.success("🔗 Connected to live Outscraper API.")

    # Scrape history manager
    st.sidebar.title("Search History 📁")
    scrapes_history = load_scrapes()
    
    selected_history = None
    if scrapes_history:
        history_options = []
        for s in scrapes_history:
            dt = s.get("timestamp", "Unknown Date")
            cat = s.get("category", "Any")
            count = len(s.get("results", []))
            mode_lbl = "Demo" if s.get("mode") == "Demo Mode" else "Live"
            history_options.append(f"{dt} | {cat} ({count} leads) [{mode_lbl}]")
            
        history_choice = st.sidebar.selectbox(
            "Load past search results",
            options=["None"] + history_options
        )
        
        if history_choice != "None":
            choice_idx = history_options.index(history_choice)
            selected_history = scrapes_history[choice_idx]
            st.sidebar.success("Scrape run loaded from history!")
    else:
        st.sidebar.info("No saved history found.")

    # Academy branding settings (used for cold emails)
    st.sidebar.title("Academy Branding 🎓")
    academy_name = st.sidebar.text_input("Academy Name", value="Vishwa Computer Academy")
    sender_name = st.sidebar.text_input("Sender Name", value="Dhrumil Prajapati")
    sender_title = st.sidebar.text_input("Designation", value="Admissions Director")
    sender_phone = st.sidebar.text_input("Contact Number", value="+91 99887 76655")
    sender_email = st.sidebar.text_input("Contact Email", value="admissions@vishwacomputer.com")

    # Initialize current leads in session state if not loaded
    if "current_leads" not in st.session_state:
        st.session_state.current_leads = []
        st.session_state.current_query = {}

    # Load from history if selected
    if selected_history is not None:
        st.session_state.current_leads = selected_history.get("results", [])
        st.session_state.current_query = {
            "category": selected_history.get("category"),
            "location": selected_history.get("location"),
            "max_results": selected_history.get("max_results"),
            "loaded_from_history": True
        }

    # --- Main Panel Search ---
    st.markdown("<h3 style='margin-bottom:0.8rem; color:#1E3A8A;'>🔍 B2B Lead Finder</h3>", unsafe_allow_html=True)
    
    col1, col2, col3, col4 = st.columns([2.5, 2.5, 1.5, 2])
    
    with col1:
        categories = [
            "Educational Institution",
            "School",
            "High School",
            "College",
            "University",
            "Computer training school",
            "Coaching centre",
            "Custom Category"
        ]
        
        # Determine index of category if running from history
        default_cat_idx = 0
        hist_cat = st.session_state.current_query.get("category", "")
        if hist_cat in categories:
            default_cat_idx = categories.index(hist_cat)
            
        selected_cat = st.selectbox(
            "Business Category",
            options=categories,
            index=default_cat_idx
        )
        
        custom_category = ""
        if selected_cat == "Custom Category":
            custom_category = st.text_input("Enter Custom Category", placeholder="e.g. Vocational institute")
            
    with col2:
        loc_val = st.session_state.current_query.get("location", "Ahmedabad, Gujarat, India")
        search_location = st.text_input("Location (City / Area)", value=loc_val)
        
    with col3:
        max_res_val = st.session_state.current_query.get("max_results", 20)
        max_results = st.number_input("Max Results", min_value=1, max_value=500, value=max_res_val)
        
    with col4:
        st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
        scan_clicked = st.button("Start Scan 🚀", use_container_width=True, type="primary")

    # Process search click
    if scan_clicked:
        search_category = custom_category if selected_cat == "Custom Category" else selected_cat
        if not search_category.strip():
            st.error("Please specify a business category.")
            return
            
        with st.spinner("Scraping and enriching leads... This may take a moment."):
            try:
                leads = []
                if demo_mode:
                    # Simulated scrape delay
                    import time
                    time.sleep(2)
                    leads = get_mock_scrape(search_category, max_results)
                else:
                    leads = run_live_scrape(api_key, search_category, search_location, max_results)
                
                # Update Session State
                st.session_state.current_leads = leads
                st.session_state.current_query = {
                    "category": search_category,
                    "location": search_location,
                    "max_results": max_results,
                    "loaded_from_history": False
                }
                
                # Save to History
                session_data = {
                    "id": str(uuid.uuid4()),
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "category": search_category,
                    "location": search_location,
                    "max_results": max_results,
                    "mode": "Demo Mode" if demo_mode else "Live Mode",
                    "results": leads
                }
                save_scrape_session(session_data)
                
                st.success(f"Successfully scraped and enriched {len(leads)} B2B leads!")
                
            except Exception as e:
                st.error(f"Failed to scrape: {str(e)}")

    # Display Results if available
    leads_list = st.session_state.current_leads
    
    if leads_list:
        st.markdown("<hr style='margin: 1.5rem 0;'>", unsafe_allow_html=True)
        
        # Main section layout: Table on left, details pane on right
        layout_left, layout_right = st.columns([5.5, 4.5])
        
        with layout_left:
            st.markdown("<h4 style='color:#1E3A8A; margin-bottom: 0.8rem;'>Leads Table</h4>", unsafe_allow_html=True)
            
            # Format data for DataFrame view
            rows = []
            for item in leads_list:
                name = item.get("name", "N/A")
                cat = item.get("category", "N/A")
                if not cat and item.get("type"):
                    cat = item.get("type")
                phone = item.get("phone", "N/A")
                website = item.get("site") or item.get("website") or "N/A"
                zipcode = item.get("postal_code", "N/A")
                
                # Email resolution - use the comprehensive extractor
                all_emails = extract_emails_from_lead(item)
                email = all_emails[0] if all_emails else ""
                
                rows.append({
                    "Business Name": name,
                    "Category": cat,
                    "Phone": phone,
                    "Website": website,
                    "Zip Code": zipcode,
                    "Email": email if email else "-",
                    "Status": "SCRAPED"
                })
                
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
            
            # Export CSV block
            csv_data = df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Export Leads to CSV",
                data=csv_data,
                file_name=f"educational_leads_{datetime.date.today()}.csv",
                mime="text/csv",
                use_container_width=True
            )
            
        with layout_right:
            st.markdown("<h4 style='color:#1E3A8A; margin-bottom: 0.8rem;'>Lead Details & Outreach</h4>", unsafe_allow_html=True)
            
            # Select specific lead
            lead_names = [item.get("name", "N/A") for item in leads_list]
            selected_lead_name = st.selectbox("Select a Lead to inspect:", options=lead_names)
            
            # Find the chosen lead object
            selected_lead = None
            for item in leads_list:
                if item.get("name") == selected_lead_name:
                    selected_lead = item
                    break
                    
            if selected_lead:
                # Outer panel card wrapper
                st.markdown(f"""
                    <div style='background-color:#F8FAFC; border:1px solid #E2E8F0; padding:1rem; border-radius:8px;'>
                        <h4 style='margin:0 0 0.5rem 0; color:#1E3A8A;'>{selected_lead.get('name')}</h4>
                        <p style='color:#4B5563; font-size:0.9rem; margin:0;'>Primary Category: <strong>{selected_lead.get('category') or selected_lead.get('type') or 'Educational'}</strong></p>
                    </div>
                """, unsafe_allow_html=True)
                st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
                
                # Tabs
                tab_info, tab_reviews, tab_email = st.tabs(["📞 Contact Info", "⭐ Reviews", "✉️ Academy Cold Email"])
                
                with tab_info:
                    phone_no = selected_lead.get("phone", "N/A")
                    site_url = selected_lead.get("site") or selected_lead.get("website") or "N/A"
                    full_addr = selected_lead.get("full_address") or selected_lead.get("address") or "N/A"
                    pcode = selected_lead.get("postal_code", "N/A")
                    
                    emails = extract_emails_from_lead(selected_lead)
                            
                    st.write(f"**Phone:** {phone_no}")
                    if site_url != "N/A":
                        st.markdown(f"**Website:** [Visit Site]({site_url}) ({site_url})")
                    else:
                        st.write("**Website:** N/A")
                        
                    st.write(f"**Address:** {full_addr}")
                    st.write(f"**Postal/Zip Code:** {pcode}")
                    
                    # Emails Section
                    st.markdown("---")
                    st.markdown("**Discovered Emails:**")
                    if emails:
                        for email_address in emails:
                            st.markdown(f"- <span class='email-pill'>{email_address}</span>", unsafe_allow_html=True)
                    else:
                        st.write("No email addresses discovered on website.")
                        
                    # Social Links
                    st.markdown("---")
                    st.markdown("**Social Profiles:**")
                    social_found = False
                    for key in ["facebook", "linkedin", "instagram", "twitter"]:
                        val = selected_lead.get(key, "")
                        if val:
                            st.markdown(f"- **{key.capitalize()}:** [{val}]({val})")
                            social_found = True
                    if not social_found:
                        st.write("No social media profiles linked.")
                        
                    # Coordinates
                    lat = selected_lead.get("latitude")
                    lon = selected_lead.get("longitude")
                    if lat and lon:
                        st.markdown("---")
                        maps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
                        st.markdown(f"**Geographical Location:** [View on Google Maps]({maps_link}) (`{lat}, {lon}`)")
                
                with tab_reviews:
                    rating = selected_lead.get("rating", "N/A")
                    reviews_count = selected_lead.get("reviews", "N/A")
                    st.markdown(f"### ⭐ {rating} / 5.0")
                    st.write(f"**Total Google Reviews:** {reviews_count}")
                    
                    # Mock breakdown for aesthetics
                    if isinstance(rating, (int, float)):
                        st.progress(float(rating)/5.0, text="Rating Score")
                    
                    # Direct link
                    google_id = selected_lead.get("google_id")
                    if google_id:
                        r_link = f"https://www.google.com/maps/place/?q=place_id:{google_id}"
                        st.markdown(f"[🔗 Open Google Maps Profile]({r_link})")
                        
                with tab_email:
                    st.markdown("**Select Academy Proposal Template:**")
                    template_type = st.selectbox(
                        "Outreach Template",
                        options=[
                            "Academic Collaboration Proposal (IT/Coding Labs)",
                            "Free Digital Literacy Workshop Offer",
                            "Career Guidance Course Affiliation"
                        ]
                    )
                    
                    # Get emails for personalization
                    to_email = emails[0] if emails else "[Email Address]"
                    lead_name = selected_lead.get("name", "Institute Name")
                    lead_area = selected_lead.get("full_address", "Ahmedabad").split(",")[0]
                    if not lead_area:
                        lead_area = "Ahmedabad"
                        
                    # Substitute fields in templates
                    if template_type == "Academic Collaboration Proposal (IT/Coding Labs)":
                        subject = f"Proposal for Digital Skills & Coding Collaboration: {academy_name} & {lead_name}"
                        body = f"""Dear Director / Principal,

I hope this email finds you well. 

My name is {sender_name}, and I am the {sender_title} at {academy_name} in Ahmedabad. We specialize in providing industry-ready training in essential computing skills, Python programming, web development, and AI tools.

We have been following the academic excellence of {lead_name} in the {lead_area} region. We would like to propose a formal collaboration to establish a co-branded "IT and Digital Coding Lab" or execute specialized extracurricular training classes for your students directly within your campus. 

This tie-up will:
1. Provide students with practical, certified coding courses that complement their academic curriculum.
2. Equip them with foundational skills for modern jobs (Web Development, Python, and AI Literacy).
3. Require zero operational hassle or capital expenditure from {lead_name}.

We would love to arrange a short 10-minute introductory call or meeting this week to share how we can collaborate. Could you please let us know a convenient time, or direct us to the appropriate authority?

Thank you for your time and consideration.

Warm regards,

{sender_name}
{sender_title}
{academy_name}
📞 {sender_phone}
✉️ {sender_email}"""

                    elif template_type == "Free Digital Literacy Workshop Offer":
                        subject = f"Complimentary Coding & AI Seminar for students of {lead_name}"
                        body = f"""Dear Principal / Administrator,

Greetings from {academy_name}.

As technology continues to reshape every profession, introducing students to logical coding concepts and artificial intelligence tools has become more crucial than ever. 

To support your students in this transition, {academy_name} is pleased to offer a **Complimentary 2-Hour Interactive Coding & AI Workshop** for the students of {lead_name}. 

**Workshop Details:**
* Topic: "Building Your First Web App & The Future of AI"
* Audience: Recommended for High School / College Students
* Mode: Practical Hands-On / Interactive Seminar (at your school/college lab or auditorium)
* Cost: Completely Free of Cost (CSR initiative by {academy_name})

Our instructors will guide your students through live coding blocks and show them how logical computing works. At the end of the session, students will receive digital participation certificates.

We can coordinate with your technical head to fit this workshop into your schedule. Would you be open to hosting this seminar in the coming weeks?

Best regards,

{sender_name}
{sender_title}
{academy_name}
📞 {sender_phone}
✉️ {sender_email}"""

                    else:  # Career Guidance Course Affiliation
                        subject = f"Specialized IT Course Affiliation & Placement Tie-Up - {lead_name}"
                        body = f"""Dear Placement Cell / Counseling Department,

I am writing to you on behalf of {academy_name}, one of Ahmedabad's leading vocational IT training institutes. 

We provide advanced, placement-oriented certification programs in software engineering, frontend web design, full-stack development, and digital marketing. 

We would like to discuss an admission tie-up and affiliation program for the students of {lead_name}:
1. **Career Counseling Seminars:** Free guidance sessions for your outgoing students about careers in the IT and software industries.
2. **Affiliated Scholarships:** A special 15% tuition waiver for any student enrolling in our advanced computer courses through {lead_name}'s reference.
3. **Joint Placement Support:** Co-hosting recruitment drives to help your alumni secure positions in leading local IT companies.

We would be honored to partner with {lead_name} to bridge the gap between academic education and professional employment. 

Could we schedule a quick call to discuss how we can establish this referral and training tie-up?

Sincerely,

{sender_name}
{sender_title}
{academy_name}
📞 {sender_phone}
✉️ {sender_email}"""

                    # Render Subject & Email
                    st.markdown(f"**To:** `{to_email}`")
                    st.text_input("Subject Line", value=subject, disabled=True)
                    st.markdown("**Email Body:**")
                    st.code(body, language="text")
                    st.info("💡 You can click the copy button in the top-right corner of the email body code-box to copy it instantly.")
    
    else:
        # Placeholder view if no scan has been run
        st.markdown("<hr style='margin: 1.5rem 0;'>", unsafe_allow_html=True)
        st.info("👈 Enter search options above and click **Start Scan** to find educational leads in Ahmedabad. (Demo mode is active by default to preview results).")

if __name__ == "__main__":
    main()
