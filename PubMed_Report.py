import os
import telebot
import datetime as dt
from google import genai
import warnings
import requests
import time
import xml.etree.ElementTree as ET
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

warnings.filterwarnings('ignore')

# Ympäristömuuttujat
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def get_robust_request(url, params=None):
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    try:
        return session.get(url, params=params, timeout=15)
    except Exception as e:
        print(f"⚠️ Verkkoyhteys epäonnistui: {e}")
        return None

def fetch_pubmed_abstracts():
    print("🔬 Haetaan tuoreita tutkimuksia PubMedistä...")
    
    # Hakutermit (Voima, Kestävyys, Palautuminen/Uni, Ravinteet) ja ihmiskokeet
    search_query = (
        '("muscle hypertrophy"[Title/Abstract] OR "strength training"[Title/Abstract] OR '
        '"rate of force development"[Title/Abstract] OR "VO2 max"[Title/Abstract] OR '
        '"cycling economy"[Title/Abstract] OR "endurance performance"[Title/Abstract] OR '
        '"heart rate variability"[Title/Abstract] OR "sleep architecture"[Title/Abstract] OR '
        '"icosapent ethyl"[Title/Abstract] OR "eicosapentaenoic acid"[Title/Abstract] OR '
        '"psyllium"[Title/Abstract] OR "whey protein"[Title/Abstract] OR '
        '"protein supplementation"[Title/Abstract] OR "creatine monohydrate"[Title/Abstract] OR '
        '"beta-alanine"[Title/Abstract]") '
        'AND (human[Filter])'
    )
    
    # 1. Etsitään julkaisujen ID:t (PMID) vain edellisen vuorokauden ajalta (reldate: 1)
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": search_query,
        "retmode": "json",
        "reldate": 1, # Haetaan vain aidosti uudet
        "retmax": 20  # 20 riittää tekoälylle
        # "sort": "date" -> Poistettu tahallaan, jotta saadaan lisäysjärjestyksessä!
    }
    
    res = get_robust_request(search_url, params=search_params)
    if not res or res.status_code != 200:
        print("❌ PubMed-haku epäonnistui.")
        return []

    data = res.json()
    id_list = data.get("esearchresult", {}).get("idlist", [])
    
    if not id_list:
        print("ℹ️ Ei uusia tutkimuksia tällä hakuehdolla viimeiseen vuorokauteen.")
        return []

    # 2. Haetaan löydettyjen julkaisujen abstraktit (tiivistelmät)
    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    fetch_params = {
        "db": "pubmed",
        "id": ",".join(id_list),
        "retmode": "xml"
    }
    
    f_res = get_robust_request(fetch_url, params=fetch_params)
    if not f_res or f_res.status_code != 200:
        return []

    # XML-jäsentäminen abstraktien ja otsikoiden kaivamiseksi
    abstracts_data = []
    try:
        root = ET.fromstring(f_res.content)
        for article in root.findall('.//PubmedArticle'):
            pmid_el = article.find('.//PMID')
            title_el = article.find('.//ArticleTitle')
            
            if pmid_el is None or title_el is None:
                continue
                
            pmid = pmid_el.text
            title = "".join(title_el.itertext()).strip()
            
            abstract_text = ""
            abstract_elements = article.findall('.//AbstractText')
            for el in abstract_elements:
                if el.text:
                    abstract_text += "".join(el.itertext()) + " "
            
            if abstract_text.strip():
                abstracts_data.append({
                    "pmid": pmid,
                    "title": title,
                    "abstract": abstract_text.strip()
                })
    except Exception as e:
        print(f"⚠️ XML-jäsentämisvirhe: {e}")

    return abstracts_data

def generate_science_report():
    if not TG_TOKEN or not TG_CHAT_ID:
        print("❌ Telegram-tunnukset puuttuvat!")
        return

    bot = telebot.TeleBot(TG_TOKEN)
    studies = fetch_pubmed_abstracts()
    
    if not studies:
        print("Ei uutta analysoitavaa.")
        return

    # Koostetaan tekoälylle lähetettävä materiaali
    ai_input = "Tässä on PubMedistä haetut tuoreet tutkimustiivistelmät:\n\n"
    for s in studies:
        ai_input += f"ID: {s['pmid']}\nOTSIKKO: {s['title']}\nTIIVISTELMÄ: {s['abstract']}\n\n"

    system_prompt = """
    Rooli: Huipputason liikuntatieteilijä, kardiologiaan ja metaboliaan perehtynyt kliininen fysiologi sekä urheiluravitsemuksen asiantuntija.
    TEHTÄVÄ: Saat listan tuoreista PubMed-tutkimuksista. Tehtäväsi on valita 1-4 kaikkein mielenkiintoisinta ja käytännönläheisintä löydöstä, jotka liittyvät voimaharjoitteluun, kestävyysurheiluun, uneen, palautumiseen tai avainlisäravinteisiin (kuten E-EPA, kuidut/psyllium, proteiinit ja kreatiini). Kirjoita niistä iskevä, suomenkielinen aamuraportti.
    
    SÄÄNNÖT:
    1. Aloita otsikolla: "🧬 **TIEDEKATSAUS [Tämän päivän päivämäärä]**"
    2. Valitse vain helmet. Keskity ihmiskokeisiin ja jätä merkityksettömät korrelaatiotutkimukset pois.
    3. Kirjoita jokaisesta valitusta tutkimuksesta lyhyt, helposti pureskeltava yhteenveto ja lisää "Käytännön sovellus" -osio (Miten hyödyntää tietoa treenissä, ravitsemuksessa, levossa tai lipidiprofiilin optimoinnissa).
    4. SISÄLLYTTÄÄ LINKIT: Laita loppuun "🔗 **Linkit alkuperäisiin tutkimuksiin:**" muodossa https://pubmed.ncbi.nlm.nih.gov/[ID]/
    5. Vastaa tiiviisti, analyyttisesti ja täysin ilman kaupallista markkinointihypeä. Älä ylitä Telegramin merkkirajaa.
    """

    print("🤖 Pyydetään Geminiltä asiantuntija-analyysi...")
    client = genai.Client(api_key=GEMINI_KEY)
    
    response = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=ai_input,
                config=genai.types.GenerateContentConfig(system_instruction=system_prompt)
            )
            break
        except Exception as e: 
            print(f"API-virhe, yritetään uudelleen: {e}")
            time.sleep(10)
    
    if response and response.text:
        msg_text = response.text
        for i in range(0, len(msg_text), 4000):
            bot.send_message(TG_CHAT_ID, msg_text[i:i+4000])
            time.sleep(1)
        print("✅ PubMed-aamuraportti lähetetty Telegramiin!")

if __name__ == "__main__":
    generate_science_report()
