# Author: Blake Rayvid <https://github.com/brayvid/based-news>

import os
import sys

# Set number of threads for various libraries to 1 if parallelism is not permitted on your system
# os.environ["OPENBLAS_NUM_THREADS"] = "1"
# os.environ["OMP_NUM_THREADS"] = "1"
# os.environ["MKL_NUM_THREADS"] = "1"
# os.environ["NUMEXPR_NUM_THREADS"] = "1"

# Define paths and URLs for local files and remote configuration.
# Robust BASE_DIR definition
try:
    BASE_DIR = os.path.dirname(os.path.realpath(__file__))
except NameError:  # __file__ is not defined, e.g., in interactive shell
    BASE_DIR = os.getcwd()

HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
DIGEST_STATE_FILE = os.path.join(BASE_DIR, "content.json") 

CONFIG_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTWCrmL5uXBJ9_pORfhESiZyzD3Yw9ci0Y-fQfv0WATRDq6T8dX0E7yz1XNfA6f92R7FDmK40MFSdH4/pub?gid=446667252&single=true&output=csv"
TOPICS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTWCrmL5uXBJ9_pORfhESiZyzD3Yw9ci0Y-fQfv0WATRDq6T8dX0E7yz1XNfA6f92R7FDmK40MFSdH4/pub?gid=0&single=true&output=csv"
KEYWORDS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTWCrmL5uXBJ9_pORfhESiZyzD3Yw9ci0Y-fQfv0WATRDq6T8dX0E7yz1XNfA6f92R7FDmK40MFSdH4/pub?gid=314441026&single=true&output=csv"
OVERRIDES_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTWCrmL5uXBJ9_pORfhESiZyzD3Yw9ci0Y-fQfv0WATRDq6T8dX0E7yz1XNfA6f92R7FDmK40MFSdH4/pub?gid=1760236101&single=true&output=csv"

# Import all required libraries
import csv
import html
import logging
import json
import re
import ast
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import requests
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from nltk.stem import PorterStemmer, WordNetLemmatizer
from dotenv import load_dotenv
import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool 
import subprocess
from proto.marshal.collections.repeated import RepeatedComposite
from proto.marshal.collections.maps import MapComposite

# Initialize logging immediately to capture all runtime info
log_path = os.path.join(BASE_DIR, "logs/digest.log") 
os.makedirs(os.path.dirname(log_path), exist_ok=True)
logging.basicConfig(filename=log_path, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info(f"Script started at {datetime.now()}")

# Initialize NLP tools and load environment variables from .env file.
stemmer = PorterStemmer()
lemmatizer = WordNetLemmatizer()
load_dotenv()

# Download nltk resources
from nltk.data import find
import nltk

if os.getenv('CI'):
    nltk_data_dir = os.path.join(BASE_DIR, "nltk_data")
    os.makedirs(nltk_data_dir, exist_ok=True)
    nltk.data.path.append(nltk_data_dir)
else:
    nltk.data.path.append(os.path.expanduser("~/nltk_data"))


def ensure_nltk_data():
    for resource in ['wordnet', 'omw-1.4']:
        try:
            find(f'corpora/{resource}')
            logging.info(f"NLTK resource '{resource}' found.")
        except LookupError:
            logging.info(f"NLTK resource '{resource}' not found. Attempting download to {nltk.data.path[-1]}...")
            try:
                nltk.download(resource, download_dir=nltk.data.path[-1])
                logging.info(f"Successfully downloaded NLTK resource '{resource}'.")
            except Exception as e:
                logging.error(f"Failed to download NLTK resource '{resource}': {e}")
                print(f"Failed to download {resource}: {e}")

ensure_nltk_data()

def load_config_from_sheet(url):
    config = {}
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        lines = response.text.splitlines()
        reader = csv.reader(lines)
        next(reader, None)  # skip header
        for row in reader:
            if len(row) >= 2:
                key = row[0].strip()
                val = row[1].strip()
                try:
                    if '.' in val and not val.startswith('0') and val.count('.') == 1:
                        config[key] = float(val)
                    else:
                        config[key] = int(val)
                except ValueError:
                    if val.lower() == 'true':
                        config[key] = True
                    elif val.lower() == 'false':
                        config[key] = False
                    else:
                        config[key] = val
        logging.info(f"Config loaded successfully from {url}")
        return config
    except Exception as e:
        logging.error(f"Failed to load config from {url}: {e}")
        return None

CONFIG = load_config_from_sheet(CONFIG_CSV_URL)
if CONFIG is None:
    logging.critical("Fatal: Unable to load CONFIG from sheet. Exiting.")
    sys.exit(1)

MAX_ARTICLE_HOURS = int(CONFIG.get("MAX_ARTICLE_HOURS", 6))
MAX_TOPICS = int(CONFIG.get("MAX_TOPICS", 10)) 
MAX_ARTICLES_PER_TOPIC = int(CONFIG.get("MAX_ARTICLES_PER_TOPIC", 1))
DEMOTE_FACTOR = float(CONFIG.get("DEMOTE_FACTOR", 0.5))
MATCH_THRESHOLD = float(CONFIG.get("DEDUPLICATION_MATCH_THRESHOLD", 0.4))
GEMINI_MODEL_NAME = CONFIG.get("GEMINI_MODEL_NAME", "gemini-1.5-flash") 
STALE_TOPIC_THRESHOLD_HOURS = int(CONFIG.get("STALE_TOPIC_THRESHOLD_HOURS", 72)) # Not used in this "snapshot" model

USER_TIMEZONE = CONFIG.get("TIMEZONE", "America/New_York")
try:
    ZONE = ZoneInfo(USER_TIMEZONE)
except Exception:
    logging.warning(f"Invalid TIMEZONE '{USER_TIMEZONE}' in config. Falling back to 'America/New_York'")
    ZONE = ZoneInfo("America/New_York")

def load_csv_weights(url):
    weights = {}
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        lines = response.text.splitlines()
        reader = csv.reader(lines)
        next(reader, None)
        for row in reader:
            if len(row) >= 2:
                try:
                    weights[row[0].strip()] = int(row[1])
                except ValueError:
                    logging.warning(f"Skipping invalid weight in {url}: {row}")
                    continue
        logging.info(f"Weights loaded successfully from {url}")
        return weights
    except Exception as e:
        logging.error(f"Failed to load weights from {url}: {e}")
        return None

def load_overrides(url):
    overrides = {}
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        reader = csv.reader(response.text.splitlines())
        next(reader, None)
        for row in reader:
            if len(row) >= 2:
                overrides[row[0].strip().lower()] = row[1].strip().lower()
        logging.info(f"Overrides loaded successfully from {url}")
        return overrides
    except Exception as e:
        logging.error(f"Failed to load overrides from {url}: {e}")
        return None

TOPIC_WEIGHTS = load_csv_weights(TOPICS_CSV_URL)
KEYWORD_WEIGHTS = load_csv_weights(KEYWORDS_CSV_URL)
OVERRIDES = load_overrides(OVERRIDES_CSV_URL)

if None in (TOPIC_WEIGHTS, KEYWORD_WEIGHTS, OVERRIDES):
    logging.critical("Fatal: Failed to load topics, keywords, or overrides. Exiting.")
    sys.exit(1)

def normalize(text):
    words = re.findall(r'\b\w+\b', text.lower())
    stemmed = [stemmer.stem(w) for w in words]
    lemmatized = [lemmatizer.lemmatize(w) for w in stemmed]
    return " ".join(lemmatized)

def is_in_history(article_title, history):
    norm_title_tokens = set(normalize(article_title).split())
    if not norm_title_tokens: return False

    for articles_in_topic in history.values():
        for past_article_data in articles_in_topic:
            past_title = past_article_data.get("title", "")
            past_tokens = set(normalize(past_title).split())
            if not past_tokens:
                continue
            intersection_len = len(norm_title_tokens.intersection(past_tokens))
            union_len = len(norm_title_tokens.union(past_tokens))
            if union_len == 0: continue
            similarity = intersection_len / union_len
            if similarity >= MATCH_THRESHOLD:
                logging.debug(f"Article '{article_title}' matched past article '{past_title}' with similarity {similarity:.2f}")
                return True
    return False

def to_user_timezone(dt):
    return dt.astimezone(ZONE)

def fetch_articles_for_topic(topic, max_articles=10): 
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(topic)}"
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        time_cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=MAX_ARTICLE_HOURS)
        articles = []
        for item in root.findall("./channel/item"):
            title_element = item.find("title")
            title = title_element.text if title_element is not None and title_element.text else "No title"
            link_element = item.find("link")
            link = link_element.text if link_element is not None and link_element.text else None
            pubDate_element = item.find("pubDate")
            pubDate = pubDate_element.text if pubDate_element is not None and pubDate_element.text else None
            if not link or not pubDate:
                logging.warning(f"Skipping article with missing link or pubDate for topic '{topic}': Title '{title}'")
                continue
            try:
                pub_dt_utc = parsedate_to_datetime(pubDate)
                if pub_dt_utc.tzinfo is None:
                    pub_dt_utc = pub_dt_utc.replace(tzinfo=ZoneInfo("UTC"))
                else:
                    pub_dt_utc = pub_dt_utc.astimezone(ZoneInfo("UTC"))
            except Exception as e:
                logging.warning(f"Could not parse pubDate '{pubDate}' for article '{title}': {e}")
                continue
            if pub_dt_utc <= time_cutoff:
                continue
            articles.append({"title": title.strip(), "link": link, "pubDate": pubDate})
            if len(articles) >= max_articles:
                break
        logging.info(f"Fetched {len(articles)} articles for topic '{topic}'")
        return articles
    except requests.exceptions.RequestException as e:
        logging.warning(f"Request failed for topic {topic} articles: {e}")
        return []
    except ET.ParseError as e:
        logging.warning(f"Failed to parse XML for topic {topic}: {e}")
        return []
    except Exception as e:
        logging.error(f"Unexpected error fetching articles for {topic}: {e}")
        return []

def build_user_preferences(topics, keywords, overrides):
    preferences = []
    if topics:
        preferences.append("User topics (ranked 1-5 in importance, 5 is most important):")
        for topic, score in sorted(topics.items(), key=lambda x: -x[1]):
            preferences.append(f"- {topic}: {score}")
    if keywords:
        preferences.append("\nHeadline keywords (ranked 1-5 in importance, 5 is most important):")
        for keyword, score in sorted(keywords.items(), key=lambda x: -x[1]):
            preferences.append(f"- {keyword}: {score}")
    banned = [k for k, v in overrides.items() if v == "ban"]
    demoted = [k for k, v in overrides.items() if v == "demote"]
    if banned:
        preferences.append("\nBanned terms (must not appear in topics or headlines):")
        preferences.extend(f"- {term}" for term in banned)
    if demoted:
        preferences.append(f"\nDemoted terms (consider headlines with these terms {DEMOTE_FACTOR} times less important to the user, all else equal):")
        preferences.extend(f"- {term}" for term in demoted)
    return "\n".join(preferences)

def safe_parse_json(raw_json_string: str) -> dict:
    if not raw_json_string:
        logging.warning("safe_parse_json received empty string.")
        return {}
    text = raw_json_string.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    if not text:
        logging.warning("JSON string is empty after stripping wrappers.")
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logging.warning(f"Initial JSON.loads failed: {e}. Attempting cleaning.")
        text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        text = re.sub(r",\s*([\]}])", r"\1", text)
        text = text.replace("True", "true").replace("False", "false").replace("None", "null")
        try:
            parsed_data = ast.literal_eval(text)
            if isinstance(parsed_data, dict):
                return parsed_data
            else: 
                logging.warning(f"ast.literal_eval parsed to non-dict type: {type(parsed_data)}. Raw: {text[:100]}")
                return {}
        except (ValueError, SyntaxError, TypeError) as e_ast:
            logging.warning(f"ast.literal_eval also failed: {e_ast}. Trying regex for quotes.")
            try:
                text = re.sub(r'(?<=([{,]\s*))([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'"\1":', text)
                text = re.sub(r":\s*'([^']*)'", r': "\1"', text)
                return json.loads(text)
            except json.JSONDecodeError as e2:
                logging.error(f"JSON.loads failed after all cleaning attempts: {e2}. Raw content (first 500 chars): {raw_json_string[:500]}")
                return {}

digest_tool_schema = {
    "type": "object",
    "properties": {
        "selected_digest_entries": {
            "type": "array",
            "description": (
                f"A list of selected news topics. Each entry in the list should be an object "
                f"containing a 'topic_name' (string) and 'headlines' (a list of strings). "
                f"Select up to {MAX_TOPICS} topics, and for each topic, select up to "
                f"{MAX_ARTICLES_PER_TOPIC} of the most relevant headlines."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "topic_name": {
                        "type": "string",
                        "description": "The name of the news topic (e.g., 'Technology', 'Climate Change')."
                    },
                    "headlines": {
                        "type": "array",
                        "items": {"type": "string", "description": "A selected headline string for this topic."},
                        "description": f"A list of up to {MAX_ARTICLES_PER_TOPIC} most important headline strings for this topic."
                    }
                },
                "required": ["topic_name", "headlines"]
            }
        }
    },
    "required": ["selected_digest_entries"]
}

SELECT_DIGEST_ARTICLES_TOOL = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="format_digest_selection",
            description=(
                f"Formats the selected news topics and headlines for the user's digest. "
                f"You must select up to {MAX_TOPICS} of the most important topics. "
                f"For each selected topic, return up to {MAX_ARTICLES_PER_TOPIC} most important headlines. "
                "The output should be structured as a list of objects, where each object contains a 'topic_name' "
                "and a list of 'headlines' corresponding to that topic."
            ),
            parameters=digest_tool_schema,
        )
    ]
)

def contains_banned_keyword(text, banned_terms):
    if not text: return False
    norm_text = normalize(text)
    return any(banned_term in norm_text for banned_term in banned_terms if banned_term)

def prioritize_with_gemini(headlines_to_send: dict, user_preferences: str, gemini_api_key: str) -> dict:
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL_NAME,
        tools=[SELECT_DIGEST_ARTICLES_TOOL]
    )

    prompt = (
        "You are an expert news curator. Your task is to meticulously select and deduplicate the most relevant news topics and headlines "
        "for a user's email digest. You will be given user preferences and a list of candidate articles. "
        "Your goal is to produce a concise, high-quality digest adhering to strict criteria.\n\n"
        f"User Preferences:\n{user_preferences}\n\n"
        f"Available Topics and Headlines (candidate articles):\n{json.dumps(dict(sorted(headlines_to_send.items())), indent=2)}\n\n"
        "Core Selection and Prioritization Logic:\n"
        "1.  **Topic Importance (User-Defined):** First, identify topics that align with the user's preferences and assigned importance weights (1=lowest, 5=highest). This is the primary driver for topic selection.\n"
        "2.  **Headline Newsworthiness & Relevance:** Within those topics, select headlines that are genuinely newsworthy, factual, objective, and deeply informative for a U.S. audience.\n"
        "3.  **Recency:** For developing stories with multiple updates, generally prefer the latest headline that provides the most comprehensive information, unless an earlier headline offers unique critical insight not found later.\n\n"
        "Strict Filtering Criteria (Apply these *after* initial relevance assessment):\n\n"
        "*   **Output Limits:**\n"
        f"    - Select up to {MAX_TOPICS} topics.\n"
        f"    - For each selected topic, choose up to {MAX_ARTICLES_PER_TOPIC} headlines.\n"
        "*   **Aggressive Deduplication:**\n"
        "    - CRITICAL: If multiple headlines cover the *exact same core event, announcement, or substantively similar information*, even if from different sources or under different candidate topics, select ONLY ONE. Choose the most comprehensive, authoritative, or recent version. Do not include slight rephrasing of the same news.\n"
        "*   **Geographic Focus:**\n"
        "    - Focus on national (U.S.) or major international news.\n"
        "    - AVOID news that is *solely* of local interest (e.g., specific to a small town, county, or local community event) *unless* it has clear and direct national or major international implications relevant to a U.S. audience (e.g., a local protest that gains national attention due to presidential involvement and sparks a national debate).\n"
        "*   **Banned/Demoted Content:**\n"
        "    - Strictly REJECT any headlines containing terms flagged as 'banned' in user preferences.\n"
        "    - Headlines with 'demote' terms should be *strongly deprioritized* (effectively treated as having an importance score of 0.1 on a 1-5 scale) and only selected if their relevance and importance are exceptionally high and no other suitable headlines exist for a critical user topic.\n" # Note: DEMOTE_FACTOR value is embedded here.
        "*   **Commercial Content:**\n"
        "    - REJECT advertisements.\n"
        "    - REJECT mentions of specific products/services UNLESS it's highly newsworthy criticism, a major market-moving announcement (e.g., a massive product recall by a major company), or a significant technological breakthrough discussed in a news context, not a promotional one.\n"
        "    - STRICTLY REJECT articles that primarily offer investment advice, promote specific stocks/cryptocurrencies as 'buy now' opportunities, or resemble 'hot stock tips' (e.g., \"Top X Stocks to Invest In,\" \"This Coin Will Explode,\" \"X Stocks Worth Buying\"). News about broad market trends (e.g., \"S&P 500 reaches record high\"), significant company earnings reports (without buy/sell advice), or major regulatory changes affecting financial markets IS acceptable. The key is to avoid direct or implied investment solicitation for specific securities.\n"
        "*   **Content Quality & Style:**\n"
        "    - Ensure a healthy diversity of subjects if possible within the user's preferences; do not let one single event (even if important) dominate the entire digest if other relevant news is available.\n"
        "    - PRIORITIZE content-rich, factual, objective, and neutrally-toned reporting.\n"
        "    - ACTIVELY AVOID and DEPRIORITIZE headlines that are:\n"
        "        - Sensationalist, using hyperbole, excessive superlatives (e.g., \"terrifying,\" \"decimated,\" \"gross failure\"), or fear-mongering.\n"
        "        - Purely for entertainment, celebrity gossip (unless of undeniable major national/international impact, e.g., death of a global icon), or \"fluff\" pieces lacking substantial news value (e.g., \"Recession Nails,\" \"Trump stumbles\").\n"
        "        - Clickbait (e.g., withholding key information, using vague teasers like \"You won't believe what happened next!\").\n"
        "        - Primarily opinion/op-ed pieces, especially those with inflammatory or biased language. Focus on reported news.\n"
        "        - Phrased as questions (e.g., \"Is X the new Y?\") or promoting listicles (e.g., \"5 reasons why...\"), unless the underlying content is exceptionally newsworthy and unique.\n"
        "*   **Overall Goal:** The selected articles must reflect genuine newsworthiness and be relevant to an informed general audience seeking serious, objective news updates.\n\n"
        "Chain-of-Thought Instruction (Internal Monologue):\n"
        "Before finalizing, briefly review your choices against these criteria. Ask yourself:\n"
        "- \"Is this headline truly distinct from others I've selected?\"\n"
        "- \"Is this purely local, or does it have wider significance?\"\n"
        "- \"Is this trying to sell me a stock or just reporting market news?\"\n"
        "- \"Is this headline objective, or is it heavily opinionated/sensational?\"\n\n"
        "Based on all the above, provide your selections using the 'format_digest_selection' tool."
    )

    logging.info("Sending request to Gemini for prioritization.")
    try:
        response = model.generate_content(
            prompt,
            tool_config={"function_calling_config": {"mode": "ANY", "allowed_function_names": ["format_digest_selection"]}}
        )
        
        finish_reason_display_str = "N/A"
        raw_finish_reason_value = None
        is_malformed_call_suspected = False 

        if response.candidates and hasattr(response.candidates[0], 'finish_reason'):
            raw_finish_reason_value = response.candidates[0].finish_reason
            
            if hasattr(raw_finish_reason_value, 'name'): 
                finish_reason_display_str = raw_finish_reason_value.name
            elif isinstance(raw_finish_reason_value, int):
                reason_map = {
                    0: "UNSPECIFIED", 1: "STOP", 2: "MAX_TOKENS",
                    3: "SAFETY", 4: "RECITATION", 5: "OTHER",
                    10: "MALFORMED_FUNC_CALL_INT_10" 
                }
                finish_reason_display_str = reason_map.get(raw_finish_reason_value, f"UNKNOWN_INT_REASON_{raw_finish_reason_value}")
                if raw_finish_reason_value == 10:
                    is_malformed_call_suspected = True
            else:
                finish_reason_display_str = f"UNKNOWN_REASON_TYPE_{type(raw_finish_reason_value)}"

        has_tool_call = False
        function_call_part = None 
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    function_call_part = part.function_call 
                    has_tool_call = True
                    finish_reason_display_str = "TOOL_CALLS" 
                    break 
        
        logging.info(f"Gemini response. finish_reason_display: {finish_reason_display_str}, raw_finish_reason_value: {raw_finish_reason_value}, has_tool_call: {has_tool_call}")
        
        if is_malformed_call_suspected and not has_tool_call:
            logging.error(f"Gemini indicated potential MALFORMED_FUNCTION_CALL (raw_value={raw_finish_reason_value}) and no tool call was processed. "
                          f"Prompt token count: {response.usage_metadata.prompt_token_count if response.usage_metadata else 'N/A'}. "
                          f"Full response: {response}")
            return {} 
        
        if function_call_part:
            if function_call_part.name == "format_digest_selection":
                args = function_call_part.args 
                logging.info(f"Gemini used tool 'format_digest_selection' with args (type: {type(args)}): {str(args)[:1000]}...") 
                
                if isinstance(args, MapComposite):
                    entries_list_proto = args.get("selected_digest_entries")
                elif isinstance(args, dict):
                    entries_list_proto = args.get("selected_digest_entries")
                else:
                    entries_list_proto = None

                if entries_list_proto is None or not (isinstance(entries_list_proto, list) or isinstance(entries_list_proto, RepeatedComposite)):
                    logging.warning(f"'selected_digest_entries' from Gemini is not a list/RepeatedComposite or is missing. Type: {type(entries_list_proto)}, Value: {entries_list_proto}")
                    return {}

                transformed_output = {}
                for entry_proto in entries_list_proto: 
                    if isinstance(entry_proto, (dict, MapComposite)):
                        topic_name = entry_proto.get("topic_name")
                        headlines_proto = entry_proto.get("headlines")

                        headlines_python_list = []
                        if isinstance(headlines_proto, (list, RepeatedComposite)):
                            headlines_python_list = [str(h) for h in headlines_proto if isinstance(h, (str, bytes))]
                        elif headlines_proto is not None: 
                            logging.warning(f"Headlines for topic '{topic_name}' is not a list/RepeatedComposite. Type: {type(headlines_proto)}")

                        if isinstance(topic_name, str) and topic_name.strip() and headlines_python_list:
                            topic_name_clean = topic_name.strip()
                            if topic_name_clean in transformed_output:
                                transformed_output[topic_name_clean].extend(headlines_python_list)
                            else:
                                transformed_output[topic_name_clean] = headlines_python_list
                            transformed_output[topic_name_clean] = list(dict.fromkeys(transformed_output[topic_name_clean]))
                        else:
                            logging.warning(f"Skipping invalid entry: topic '{topic_name}' (type {type(topic_name)}), headlines '{headlines_python_list}'")
                    else:
                        logging.warning(f"Skipping non-dict/MapComposite item in 'selected_digest_entries': type {type(entry_proto)}, value {entry_proto}")
                
                logging.info(f"Transformed output from Gemini tool call: {transformed_output}")
                return transformed_output
            else:
                logging.warning(f"Gemini called an unexpected tool: {function_call_part.name}")
                return {}
        elif response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            text_content = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
            if text_content.strip():
                logging.warning("Gemini did not use the tool, returned text instead. Attempting to parse.")
                logging.debug(f"Gemini raw text response: {text_content}")
                parsed_json = safe_parse_json(text_content)
                
                if "selected_digest_entries" in parsed_json and isinstance(parsed_json["selected_digest_entries"], list):
                    transformed_output = {}
                    for entry in parsed_json["selected_digest_entries"]:
                        if isinstance(entry, dict):
                            topic_name = entry.get("topic_name")
                            headlines_list = entry.get("headlines")
                            if isinstance(topic_name, str) and topic_name.strip() and isinstance(headlines_list, list):
                                valid_headlines = [h for h in headlines_list if isinstance(h, str)]
                                if valid_headlines: 
                                    topic_name_clean = topic_name.strip()
                                    if topic_name_clean in transformed_output:
                                        transformed_output[topic_name_clean].extend(valid_headlines)
                                    else:
                                        transformed_output[topic_name_clean] = valid_headlines
                                    transformed_output[topic_name_clean] = list(dict.fromkeys(transformed_output[topic_name_clean]))
                            else:
                                logging.warning(f"Skipping invalid entry in parsed text JSON: {entry}")
                        else:
                             logging.warning(f"Skipping non-dict item in parsed text 'selected_digest_entries': {entry}")
                    if transformed_output:
                        logging.info(f"Successfully parsed and transformed text response from Gemini: {transformed_output}")
                        return transformed_output
                    else:
                        logging.warning(f"Parsed text response from Gemini did not yield usable digest entries.")
                        return {}
                else:
                    logging.warning(f"Gemini text response could not be parsed into the expected digest structure. Raw text: {text_content[:500]}")
                    return {}
            else:
                 logging.warning("Gemini returned no usable function call and no parsable text content (empty parts).")
                 if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                     logging.warning(f"Prompt feedback: {response.prompt_feedback}")
                 logging.warning(f"Full Gemini response: {response}")
                 return {}
        else: 
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                logging.warning(f"Gemini response has prompt feedback: {response.prompt_feedback}")
            logging.warning(f"Gemini returned no candidates or no content parts. Full response object: {response}")
            return {}

    except Exception as e:
        logging.error(f"Error during Gemini API call or processing response: {e}", exc_info=True)
        try:
            if 'response' in locals() and response: 
                logging.error(f"Gemini response object on error (prompt_feedback): {response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'N/A'}")
                if hasattr(response, 'candidates') and response.candidates: 
                     logging.error(f"Gemini response object on error (first candidate): {response.candidates[0]}")
            else:
                logging.error("Response object not available or None at the time of error logging during exception.")
        except Exception as e_log:
            logging.error(f"Error logging response details during exception: {e_log}")
        return {}


def write_digest_html(digest_data, base_dir, current_zone):
    digest_path = os.path.join(base_dir, "public", "digest.html")
    os.makedirs(os.path.dirname(digest_path), exist_ok=True)

    html_parts = []
    # digest_data is now expected to be pre-sorted by newest article pubdate
    for topic, articles in digest_data.items(): 
        html_parts.append(f"<h3>{html.escape(topic)}</h3>\n")
        for article in articles: 
            try:
                pub_dt_orig = parsedate_to_datetime(article["pubDate"])
                pub_dt_user_tz = to_user_timezone(pub_dt_orig)
                date_str = pub_dt_user_tz.strftime("%a, %d %b %Y %I:%M %p %Z")
            except Exception as e:
                logging.warning(f"Could not parse date for article '{article['title']}': {article['pubDate']} - {e}")
                date_str = "Date unavailable"

            html_parts.append(
                f'<p>'
                f'<a href="{html.escape(article["link"])}" target="_blank">{html.escape(article["title"])}</a><br>'
                f'<small>{date_str}</small>'
                f'</p>\n'
            )
    
    last_updated_dt = datetime.now(current_zone)
    last_updated_str_for_footer = last_updated_dt.strftime("%A, %d %B %Y %I:%M %p %Z")
    
    footer_html = (
        f"<div class='timestamp' id='last-updated' style='display: none;'>" 
        f"Last updated: {last_updated_str_for_footer}"
        f"</div>\n"
    )
    html_parts.append(footer_html)

    with open(digest_path, "w", encoding="utf-8") as f:
        f.write("".join(html_parts))


def update_history_file(newly_selected_articles_by_topic, current_history, history_file_path, current_zone):
    if not newly_selected_articles_by_topic or not isinstance(newly_selected_articles_by_topic, dict):
        logging.info("No newly selected articles provided to update_history_file, or invalid format. Proceeding to prune existing history.")

    for topic, articles in newly_selected_articles_by_topic.items():
        history_key = topic.lower().replace(" ", "_") 
        if history_key not in current_history:
            current_history[history_key] = []
        
        existing_norm_titles_in_topic_history = {normalize(a.get("title","")) for a in current_history[history_key]}

        for article in articles:
            norm_title = normalize(article["title"])
            if norm_title not in existing_norm_titles_in_topic_history:
                current_history[history_key].append({
                    "title": article["title"],
                    "pubDate": article["pubDate"] 
                })
                existing_norm_titles_in_topic_history.add(norm_title)

    history_retention_days = int(CONFIG.get("HISTORY_RETENTION_DAYS", 7))
    time_limit_utc = datetime.now(ZoneInfo("UTC")) - timedelta(days=history_retention_days)
    
    for topic_key in list(current_history.keys()): 
        updated_topic_articles_in_history = []
        for article_entry in current_history[topic_key]:
            try:
                pub_dt_str = article_entry.get("pubDate")
                if not pub_dt_str: 
                    logging.warning(f"History entry for topic '{topic_key}' title '{article_entry.get('title')}' missing pubDate. Keeping.")
                    updated_topic_articles_in_history.append(article_entry)
                    continue

                pub_dt_orig = parsedate_to_datetime(pub_dt_str)
                pub_dt_utc = pub_dt_orig.astimezone(ZoneInfo("UTC")) if pub_dt_orig.tzinfo else pub_dt_orig.replace(tzinfo=ZoneInfo("UTC"))
                
                if pub_dt_utc >= time_limit_utc:
                    updated_topic_articles_in_history.append(article_entry)
            except Exception as e:
                logging.warning(f"Could not parse pubDate '{article_entry.get('pubDate')}' for history cleaning of article '{article_entry.get('title')}': {e}. Keeping entry.")
                updated_topic_articles_in_history.append(article_entry) 
        
        if updated_topic_articles_in_history:
            current_history[topic_key] = updated_topic_articles_in_history
        else:
            logging.info(f"Removing empty topic_key '{topic_key}' from history after pruning.")
            del current_history[topic_key] 

    try:
        with open(history_file_path, "w", encoding="utf-8") as f:
            json.dump(current_history, f, indent=2)
        logging.info(f"History file updated at {history_file_path}")
    except IOError as e:
        logging.error(f"Failed to write updated history file: {e}")

def perform_git_operations(base_dir, current_zone, config_obj):
    try:
        github_token = os.getenv("GITHUB_TOKEN")
        github_repository_owner_slash_repo = os.getenv("GITHUB_REPOSITORY")
        
        if not github_token or not github_repository_owner_slash_repo:
            logging.error("GITHUB_TOKEN or GITHUB_REPOSITORY not set. Cannot push to GitHub.")
            return
        
        remote_url = f"https://oauth2:{github_token}@github.com/{github_repository_owner_slash_repo}.git"
        
        commit_author_name = os.getenv("GITHUB_USER", config_obj.get("GIT_USER_NAME", "Automated Digest Bot"))
        commit_author_email = os.getenv("GITHUB_EMAIL", config_obj.get("GIT_USER_EMAIL", "bot@example.com"))

        logging.info(f"Using Git Commit Author Name: '{commit_author_name}', Email: '{commit_author_email}'")

        subprocess.run(["git", "config", "user.name", commit_author_name], check=True, cwd=base_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", commit_author_email], check=True, cwd=base_dir, capture_output=True)
        try:
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=True, cwd=base_dir, capture_output=True)
        except subprocess.CalledProcessError:
            logging.info("Failed to set-url (maybe remote 'origin' doesn't exist). Attempting to add.")
            subprocess.run(["git", "remote", "add", "origin", remote_url], check=True, cwd=base_dir, capture_output=True)

        branch_result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True, cwd=base_dir)
        current_branch = branch_result.stdout.strip()
        if not current_branch or current_branch == "HEAD":
            logging.warning(f"Could not reliably determine current branch (got '{current_branch}'). Defaulting to 'main'.")
            current_branch = "main" 
            try:
                subprocess.run(["git", "checkout", current_branch], check=True, cwd=base_dir, capture_output=True)
            except subprocess.CalledProcessError as e_checkout:
                err_msg = e_checkout.stderr.decode(errors='ignore') if e_checkout.stderr else e_checkout.stdout.decode(errors='ignore')
                logging.error(f"Failed to checkout branch '{current_branch}': {err_msg}. Proceeding with caution.")
        
        logging.info("Attempting to stash local changes before pull.")
        stash_result = subprocess.run(["git", "stash", "push", "-u", "-m", "WIP_Stash_By_Script"], capture_output=True, text=True, cwd=base_dir)
        stashed_changes = "No local changes to save" not in stash_result.stdout and stash_result.returncode == 0

        if stashed_changes:
            logging.info(f"Stashed local changes. Output: {stash_result.stdout.strip()}")
        elif stash_result.returncode != 0 :
             logging.warning(f"git stash push failed. Stdout: {stash_result.stdout.strip()}, Stderr: {stash_result.stderr.strip()}")
        else:
            logging.info("No local changes to stash.")

        logging.info(f"Attempting to pull with rebase from origin/{current_branch}...")
        pull_rebase_cmd = ["git", "pull", "--rebase", "origin", current_branch]
        pull_result = subprocess.run(pull_rebase_cmd, capture_output=True, text=True, cwd=base_dir)

        if pull_result.returncode != 0:
            logging.warning(f"'git pull --rebase' failed. Stdout: {pull_result.stdout.strip()}. Stderr: {pull_result.stderr.strip()}")
            if "CONFLICT" in pull_result.stdout or "CONFLICT" in pull_result.stderr:
                 logging.error("Rebase conflict detected during pull. Aborting rebase.")
                 subprocess.run(["git", "rebase", "--abort"], cwd=base_dir, capture_output=True)
                 if stashed_changes:
                     logging.info("Attempting to pop stashed changes after rebase abort.")
                     pop_after_abort_result = subprocess.run(["git", "stash", "pop"], cwd=base_dir, capture_output=True, text=True)
                     if pop_after_abort_result.returncode != 0:
                         logging.error(f"Failed to pop stash after rebase abort. Stderr: {pop_after_abort_result.stderr.strip()}")
                 logging.warning("Skipping push this cycle due to rebase conflict.")
                 return 
        else:
            logging.info(f"'git pull --rebase' successful. Stdout: {pull_result.stdout.strip()}")

        if stashed_changes:
            logging.info("Attempting to pop stashed changes.")
            pop_result = subprocess.run(["git", "stash", "pop"], capture_output=True, text=True, cwd=base_dir)
            if pop_result.returncode != 0:
                logging.error(f"git stash pop failed! This might indicate conflicts. Stderr: {pop_result.stderr.strip()}")
                logging.warning("Proceeding to add/commit script changes, but manual conflict resolution for stash might be needed later.")
            else:
                logging.info("Stashed changes popped successfully.")
        
        files_for_git_add = []
        history_file_abs = os.path.join(base_dir, "history.json")
        digest_state_file_abs = os.path.join(base_dir, "content.json") 
        digest_html_path_abs = os.path.join(base_dir, "public/digest.html")

        if os.path.exists(history_file_abs): files_for_git_add.append(os.path.relpath(history_file_abs, base_dir))
        if os.path.exists(digest_html_path_abs): files_for_git_add.append(os.path.relpath(digest_html_path_abs, base_dir))
        if os.path.exists(digest_state_file_abs): files_for_git_add.append(os.path.relpath(digest_state_file_abs, base_dir))
        
        if files_for_git_add:
            logging.info(f"Staging script generated/modified files: {files_for_git_add}")
            add_process = subprocess.run(["git", "add"] + files_for_git_add, 
                                         capture_output=True, text=True, cwd=base_dir) 
            if add_process.returncode != 0:
                logging.error(f"git add command for script files failed. RC: {add_process.returncode}, Stdout: {add_process.stdout.strip()}, Stderr: {add_process.stderr.strip()}")
            else:
                logging.info(f"git add successful for: {files_for_git_add}")
        else:
            logging.info("No specific script-generated files found/modified to add.")
        
        status_result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True, cwd=base_dir)
        if not status_result.stdout.strip():
            logging.info("No changes to commit after all operations. Local branch likely matches remote or no script changes.")
            return
        
        commit_message = f"Auto-update digest content - {datetime.now(current_zone).strftime('%Y-%m-%d %H:%M:%S %Z')}"
        commit_cmd = ["git", "commit", "-m", commit_message]
        commit_result = subprocess.run(commit_cmd, capture_output=True, text=True, cwd=base_dir)

        if commit_result.returncode != 0:
            if "nothing to commit" in commit_result.stdout.lower() or \
               "no changes added to commit" in commit_result.stdout.lower() or \
               "your branch is up to date" in commit_result.stdout.lower():
                logging.info(f"Commit attempt reported no new changes. Stdout: {commit_result.stdout.strip()}")
                try: 
                    local_head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=base_dir).stdout.strip()
                    remote_head_cmd_out = subprocess.run(["git", "ls-remote", "origin", f"refs/heads/{current_branch}"], capture_output=True, text=True, cwd=base_dir)
                    if remote_head_cmd_out.returncode == 0 and remote_head_cmd_out.stdout.strip():
                        remote_head = remote_head_cmd_out.stdout.split()[0].strip()
                        if local_head == remote_head:
                            logging.info(f"Local {current_branch} is same as origin/{current_branch}. No push needed.")
                            return
                    logging.info("Local commit differs from remote or remote check failed. Will attempt push.")
                except Exception as e_rev: 
                    logging.warning(f"Could not compare local/remote revisions: {e_rev}. Will attempt push.")
            else: 
                logging.error(f"git commit command failed. RC: {commit_result.returncode}, Stdout: {commit_result.stdout.strip()}, Stderr: {commit_result.stderr.strip()}")
        else:
            logging.info(f"Commit successful: {commit_result.stdout.strip()}")
        
        logging.info(f"Pushing changes to origin/{current_branch}...")
        push_cmd = ["git", "push", "origin", current_branch]
        push_result = subprocess.run(push_cmd, check=True, cwd=base_dir, capture_output=True) 
        logging.info(f"Content committed and pushed to GitHub on branch '{current_branch}'. Push output: {push_result.stdout.decode(errors='ignore').strip() if push_result.stdout else ''}")

    except subprocess.CalledProcessError as e:
        output_str = e.output.decode(errors='ignore') if hasattr(e, 'output') and e.output else ""
        stderr_str = e.stderr.decode(errors='ignore') if hasattr(e, 'stderr') and e.stderr else ""
        logging.error(f"Git operation failed: {e}. Command: '{e.cmd}'. Output: {output_str}. Stderr: {stderr_str}")
    except Exception as e:
        logging.error(f"General error during Git operations: {e}", exc_info=True)


def main():
    history = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except json.JSONDecodeError:
            logging.warning("history.json is empty or invalid. Starting with an empty history.")
        except Exception as e:
            logging.error(f"Error loading history.json: {e}. Starting with empty history.")

    # In this model, content.json is a direct snapshot of the latest run.
    
    try:
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            logging.error("Missing GEMINI_API_KEY environment variable. Exiting.")
            sys.exit(1)

        user_preferences = build_user_preferences(TOPIC_WEIGHTS, KEYWORD_WEIGHTS, OVERRIDES)
        
        headlines_to_send_to_llm = {} 
        full_articles_map_this_run = {} 
        
        banned_terms_list = [k for k, v in OVERRIDES.items() if v == "ban"]
        normalized_banned_terms = [normalize(term) for term in banned_terms_list if term] 

        articles_to_fetch_per_topic = int(CONFIG.get("ARTICLES_TO_FETCH_PER_TOPIC", 10))

        for topic_name in TOPIC_WEIGHTS:
            fetched_topic_articles = fetch_articles_for_topic(topic_name, articles_to_fetch_per_topic) 
            if fetched_topic_articles:
                current_topic_headlines_for_llm = []
                for art in fetched_topic_articles:
                    if is_in_history(art["title"], history):
                        logging.debug(f"Skipping (in history): {art['title']}")
                        continue
                    if contains_banned_keyword(art["title"], normalized_banned_terms): 
                        logging.debug(f"Skipping (banned keyword): {art['title']}")
                        continue
                    
                    current_topic_headlines_for_llm.append(art["title"])
                    norm_title_key = normalize(art["title"]) 
                    if norm_title_key not in full_articles_map_this_run:
                         full_articles_map_this_run[norm_title_key] = art
                
                if current_topic_headlines_for_llm:
                    headlines_to_send_to_llm[topic_name] = current_topic_headlines_for_llm
        
        # This will hold Gemini's output after initial processing and MAX_ARTICLES_PER_TOPIC truncation
        gemini_processed_content = {} # CORRECTED VARIABLE NAME HERE

        if not headlines_to_send_to_llm:
            logging.info("No new, non-banned, non-historical headlines available to send to LLM.")
        else:
            total_headlines_count = sum(len(v) for v in headlines_to_send_to_llm.values())
            logging.info(f"Sending {total_headlines_count} candidate headlines across {len(headlines_to_send_to_llm)} topics to Gemini.")
            
            selected_content_raw_from_llm = prioritize_with_gemini(headlines_to_send_to_llm, user_preferences, gemini_api_key)

            if not selected_content_raw_from_llm or not isinstance(selected_content_raw_from_llm, dict):
                logging.warning("Gemini returned no content or invalid format.")
            else:
                # Enforce MAX_TOPICS on Gemini's output
                if len(selected_content_raw_from_llm) > MAX_TOPICS:
                    logging.warning(f"Gemini returned {len(selected_content_raw_from_llm)} topics, which exceeds MAX_TOPICS={MAX_TOPICS}. Truncating to the first {MAX_TOPICS} topics provided by Gemini.")
                    truncated_topics_list = list(selected_content_raw_from_llm.items())[:MAX_TOPICS]
                    selected_content_raw_from_llm = dict(truncated_topics_list) 
                
                logging.info(f"Processing {len(selected_content_raw_from_llm)} topics from Gemini (after script's MAX_TOPICS truncation). Enforcing MAX_ARTICLES_PER_TOPIC={MAX_ARTICLES_PER_TOPIC}.")
                seen_normalized_titles_in_llm_output = set() 

                for topic_from_llm, titles_from_llm_untruncated in selected_content_raw_from_llm.items():
                    if not isinstance(titles_from_llm_untruncated, list):
                        logging.warning(f"LLM returned non-list for topic '{topic_from_llm}': {titles_from_llm_untruncated}. Skipping.")
                        continue
                    
                    titles_from_llm = titles_from_llm_untruncated[:MAX_ARTICLES_PER_TOPIC]
                    if len(titles_from_llm_untruncated) > MAX_ARTICLES_PER_TOPIC:
                        logging.info(f"Topic '{topic_from_llm}' from LLM had {len(titles_from_llm_untruncated)} articles, script truncated to {MAX_ARTICLES_PER_TOPIC}.")

                    current_topic_articles_for_digest = []
                    for title_from_llm in titles_from_llm: 
                        if not isinstance(title_from_llm, str):
                            logging.warning(f"LLM returned non-string headline: {title_from_llm} for topic '{topic_from_llm}'. Skipping.")
                            continue
                        
                        norm_llm_title = normalize(title_from_llm)
                        if not norm_llm_title: continue

                        if norm_llm_title in seen_normalized_titles_in_llm_output:
                            logging.info(f"Deduplicating LLM output: '{title_from_llm}' already selected under another topic by LLM this run.")
                            continue
                        
                        article_data = full_articles_map_this_run.get(norm_llm_title)
                        if article_data:
                            current_topic_articles_for_digest.append(article_data)
                            seen_normalized_titles_in_llm_output.add(norm_llm_title)
                        else: 
                            found_fallback = False
                            for stored_norm_title, stored_article_data in full_articles_map_this_run.items():
                                if norm_llm_title in stored_norm_title or stored_norm_title in norm_llm_title:
                                    if stored_norm_title not in seen_normalized_titles_in_llm_output: 
                                        current_topic_articles_for_digest.append(stored_article_data)
                                        seen_normalized_titles_in_llm_output.add(stored_norm_title) 
                                        logging.info(f"Matched LLM title '{title_from_llm}' to stored '{stored_article_data['title']}' via fallback.")
                                        found_fallback = True
                                        break
                            if not found_fallback:
                                logging.warning(f"Could not map LLM title '{title_from_llm}' (normalized: '{norm_llm_title}') back to a fetched article.")
                    
                    if current_topic_articles_for_digest:
                        gemini_processed_content[topic_from_llm] = current_topic_articles_for_digest # Use the corrected variable
        
        # Sort the selected topics by the pubDate of their first article, newest first
        final_digest_for_display_and_state = {}
        if gemini_processed_content: # Use the corrected variable name here
            topics_with_pubdates = []
            for topic_name, articles in gemini_processed_content.items(): # And here
                if not articles: 
                    continue 
                
                newest_pubdate_str = articles[0]['pubDate'] 
                try:
                    newest_pubdate_dt = parsedate_to_datetime(newest_pubdate_str)
                    if newest_pubdate_dt.tzinfo is None: 
                        newest_pubdate_dt = newest_pubdate_dt.replace(tzinfo=ZoneInfo("UTC"))
                    topics_with_pubdates.append((topic_name, articles, newest_pubdate_dt))
                except Exception as e:
                    logging.warning(f"Could not parse pubDate '{newest_pubdate_str}' for topic '{topic_name}' during sorting. Using epoch. Error: {e}")
                    topics_with_pubdates.append((topic_name, articles, datetime.min.replace(tzinfo=ZoneInfo("UTC"))))

            topics_with_pubdates.sort(key=lambda x: x[2], reverse=True)
            
            # Reconstruct the dictionary in the new sorted order
            # Python 3.7+ dicts maintain insertion order
            for topic_name, articles, _ in topics_with_pubdates:
                final_digest_for_display_and_state[topic_name] = articles
            logging.info(f"Sorted {len(final_digest_for_display_and_state)} topics by newest article pubdate for display.")
        else:
            logging.info("No content from Gemini to sort for display.")
            # final_digest_for_display_and_state is already {}
        
        # Write to HTML and content.json
        if final_digest_for_display_and_state:
            content_json_to_save = {}
            now_utc_iso = datetime.now(ZoneInfo("UTC")).isoformat() 
            
            # Iterate over the sorted dictionary to preserve order for content.json as well
            for topic, articles in final_digest_for_display_and_state.items():
                content_json_to_save[topic] = {
                    "articles": articles,
                    "last_updated_ts": now_utc_iso 
                }
            
            write_digest_html(final_digest_for_display_and_state, BASE_DIR, ZONE) # Pass the sorted dict
            logging.info(f"Digest HTML written/updated with {len(final_digest_for_display_and_state)} topics, sorted by newest article.")
            
            try:
                with open(DIGEST_STATE_FILE, "w", encoding="utf-8") as f: 
                    json.dump(content_json_to_save, f, indent=2) # Save the sorted content
                logging.info(f"Snapshot of current digest saved to {DIGEST_STATE_FILE}")
            except IOError as e:
                logging.error(f"Failed to write digest state file {DIGEST_STATE_FILE}: {e}")
        
        else: 
            digest_html_path = os.path.join(BASE_DIR, "public", "digest.html")
            if os.path.exists(digest_html_path):
                logging.info("No topics from Gemini this run. Existing digest.html (if any) is NOT modified.")
            else:
                logging.info("No topics from Gemini this run, and digest.html does not exist. It will not be created.")
            
            try: 
                with open(DIGEST_STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump({}, f, indent=2) 
                logging.info(f"Gemini provided no topics; {DIGEST_STATE_FILE} updated to empty.")
            except IOError as e:
                logging.error(f"Failed to write empty digest state file {DIGEST_STATE_FILE}: {e}")

        update_history_file(final_digest_for_display_and_state, history, HISTORY_FILE, ZONE)

        if CONFIG.get("ENABLE_GIT_PUSH", False):
            perform_git_operations(BASE_DIR, ZONE, CONFIG)
        else:
            logging.info("Git push is disabled in config. Skipping.")

    except Exception as e:
        logging.critical(f"An unhandled error occurred in main: {e}", exc_info=True)
    finally:
        logging.info(f"Script finished at {datetime.now(ZONE)}")
if __name__ == "__main__":
    main()