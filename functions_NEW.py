import requests
import pandas as pd
import re
import os
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime, timedelta

# Load environment variables
load_dotenv()
API_KEY = os.getenv("FMP_KEY")
MONGO_URI = os.getenv("MONGO_URI")

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client["financial_data"]

# -----------------------------
# Logging helper
# -----------------------------
def log_error(error_type: str, message: str, details: dict = None):
    log_entry = {
        "timestamp": datetime.utcnow(),
        "type": error_type,
        "message": message,
        "details": details or {}
    }
    db.logs.insert_one(log_entry)

# -----------------------------
# MongoDB caching helper
# -----------------------------
from datetime import datetime

def get_or_fetch(collection_name, key_filter, fetch_func, ttl_seconds):
    collection = db[collection_name]
    doc = collection.find_one(key_filter)

    if doc:
        return doc['data']

    data = fetch_func()
    if data:
        collection.insert_one({
            **key_filter,
            "data": data,
            "createdAt": datetime.utcnow()
        })

        collection.create_index(
            [("createdAt", 1)],
            expireAfterSeconds=ttl_seconds
        )
    return data


# -----------------------------
# 1. Validate stock symbol
# -----------------------------
def is_valid_symbol(symbol: str) -> bool:
    if not re.match(r"^[A-Z\.]{1,6}$", symbol.upper()):
        print(f"[ERROR] Invalid symbol format: {symbol}")
        log_error("validation", f"Invalid symbol format: {symbol}", {"symbol": symbol})
        return False

    def fetch():
        url = f"https://financialmodelingprep.com/api/v3/search?query={symbol}&limit=1&apikey={API_KEY}"
        return requests.get(url).json()

    data = get_or_fetch("search_results", {"symbol": symbol}, fetch, ttl_seconds=86400) # 1 day
    return bool(data and data[0]['symbol'].upper() == symbol.upper())


# -----------------------------
# 2. Get peers from same sector
# -----------------------------
def get_sector_peers(symbol: str, limit=5) -> list:
    def fetch_profile():
        url = f"https://financialmodelingprep.com/api/v3/profile/{symbol}?apikey={API_KEY}"
        return requests.get(url).json()

    profile = get_or_fetch("profiles", {"symbol": symbol}, fetch_profile, ttl_seconds=86400) # 1 day
    if not profile:
        print("[ERROR] Could not get profile info.")
        log_error("data_fetch", f"Could not get profile info for {symbol}", {"symbol": symbol})
        return []

    company = profile[0]
    sector = company.get('sector')

    if sector:
        def fetch_sector():
            url = f"https://financialmodelingprep.com/api/v3/stock-screener?sector={sector}&limit=50&apikey={API_KEY}"
            return requests.get(url).json()

        companies = get_or_fetch("sector_peers", {"sector": sector}, fetch_sector, ttl_seconds=172800)  # 2 days

        if companies:
            return [c['symbol'] for c in companies if c['symbol'] != symbol][:limit]

    print(f"[ERROR] No peers found using sector '{sector}'.")
    log_error("data_fetch", f"No peers found using sector '{sector}'", {"symbol": symbol, "sector": sector})
    return []


# -----------------------------
# 3. Fetch main company financials
# -----------------------------
def fetch_main_company_financials(symbol: str) -> dict:
    def fetch():
        base_url = "https://financialmodelingprep.com/api/v3"
        income = get_or_fetch("income_statement", {"symbol": symbol}, lambda: requests.get(f"{base_url}/income-statement/{symbol}?limit=2&apikey={API_KEY}").json(), ttl_seconds=172800)
        cashflow = get_or_fetch("cashflow", {"symbol": symbol}, lambda: requests.get(f"{base_url}/cash-flow-statement/{symbol}?limit=2&apikey={API_KEY}").json(), ttl_seconds=172800)
        ratios = get_or_fetch("ratios", {"symbol": symbol}, lambda: requests.get(f"{base_url}/ratios-ttm/{symbol}?apikey={API_KEY}").json(), ttl_seconds=86400)
        quote = get_or_fetch("quotes", {"symbol": symbol}, lambda: requests.get(f"{base_url}/quote/{symbol}?apikey={API_KEY}").json(), ttl_seconds=3600)

        if not income or not cashflow or not ratios or not quote:
            return None

        revenue_growth = (
            (income[0].get('revenue', 0) - income[1].get('revenue', 0)) / income[1].get('revenue', 1)
        ) * 100 if income and len(income) > 1 else 0
        free_cash_flow = cashflow[0].get('freeCashFlow', 0) / 1000000

        return {
            'Revenue Growth (%)': revenue_growth,
            'Net Profit Margin (%)': ratios[0].get('netProfitMarginTTM', 0) * 100,
            'ROE (%)': ratios[0].get('returnOnEquityTTM', 0) * 100,
            'Debt-to-Equity': ratios[0].get('debtEquityRatioTTM', 0),
            'Free Cash Flow (M)': free_cash_flow,
            'EPS': quote[0].get('eps', 0),
            'P/E Ratio': quote[0].get('pe', 0),
            'Current Ratio': ratios[0].get('currentRatioTTM', 0)
        }

    return get_or_fetch("company_financials", {"symbol": symbol}, fetch, ttl_seconds=86400) # 1 day


# -----------------------------
# 4. Fetch peer financials
# -----------------------------
def fetch_peer_financials_batch(symbols: list) -> list:
    base_url = "https://financialmodelingprep.com/api/v3"
    data = []

    for symbol in symbols:
        print(f"\n[DEBUG] Fetching data for peer: {symbol}")
        
        # Skip non-US stocks
        if '.' in symbol:
            print(f"[DEBUG] Skipping non-US stock: {symbol}")
            continue
            
        def fetch():
            try:
                print(f"[DEBUG] Fetching quote for {symbol}")
                quote_resp = get_or_fetch("quotes", {"symbol": symbol}, lambda: requests.get(f"{base_url}/quote/{symbol}?apikey={API_KEY}").json(), ttl_seconds=3600)
                
                # Check for API error message
                if isinstance(quote_resp, dict) and 'Error Message' in quote_resp:
                    print(f"[DEBUG] API error for {symbol}: {quote_resp['Error Message']}")
                    return None
                    
                print(f"[DEBUG] Quote response: {quote_resp}")
                
                print(f"[DEBUG] Fetching ratios for {symbol}")
                ratio_resp = get_or_fetch("ratios", {"symbol": symbol}, lambda: requests.get(f"{base_url}/ratios-ttm/{symbol}?apikey={API_KEY}").json(), ttl_seconds=86400)
                
                # Check for API error message
                if isinstance(ratio_resp, dict) and 'Error Message' in ratio_resp:
                    print(f"[DEBUG] API error for {symbol}: {ratio_resp['Error Message']}")
                    return None
                    
                print(f"[DEBUG] Ratio response: {ratio_resp}")

                if not quote_resp or not ratio_resp:
                    print(f"[DEBUG] Missing data for {symbol}")
                    return None

                quote = quote_resp[0]
                ratio = ratio_resp[0]

                peer_data = {
                    'Revenue Growth (%)': None,
                    'Net Profit Margin (%)': ratio.get('netProfitMarginTTM') * 100 if ratio.get('netProfitMarginTTM') else None,
                    'ROE (%)': ratio.get('returnOnEquityTTM') * 100 if ratio.get('returnOnEquityTTM') else None,
                    'Debt-to-Equity': ratio.get('debtEquityRatioTTM'),
                    'Free Cash Flow (M)': None,
                    'EPS': quote.get('eps'),
                    'P/E Ratio': quote.get('pe'),
                    'Current Ratio': ratio.get('currentRatioTTM')
                }
                
                print(f"[DEBUG] Processed peer data for {symbol}: {peer_data}")
                return peer_data
            except Exception as e:
                print(f"[DEBUG] Error processing {symbol}: {str(e)}")
                return None

        peer_data = get_or_fetch("peer_financials", {"symbol": symbol}, fetch, ttl_seconds=86400)
        if peer_data:
            # Check if we have all required non-None values
            required_fields = ['Net Profit Margin (%)', 'ROE (%)', 'Debt-to-Equity', 'EPS', 'P/E Ratio', 'Current Ratio']
            missing_fields = [field for field in required_fields if peer_data.get(field) is None]
            
            if missing_fields:
                print(f"[DEBUG] Skipping {symbol} due to missing fields: {missing_fields}")
            else:
                print(f"[DEBUG] Adding peer data for {symbol}")
                data.append(peer_data)
        else:
            print(f"[DEBUG] No data returned for {symbol}")

    print(f"\n[DEBUG] Final peer data list: {data}")
    return data


# -----------------------------
# 5. Score calculation
# -----------------------------
def calculate_financial_score(company_data: dict, sector_data: list) -> float:
    try:
        # Debug logging
        print(f"\n[DEBUG] Company data: {company_data}")
        print(f"[DEBUG] Sector data: {sector_data}")

        # Check if we have valid data
        if not company_data or not sector_data:
            raise ValueError("Missing company or sector data")

        all_data = sector_data + [company_data]
        df = pd.DataFrame(all_data)

        # Debug logging for DataFrame
        print(f"[DEBUG] DataFrame columns: {df.columns.tolist()}")
        print(f"[DEBUG] DataFrame shape: {df.shape}")
        print(f"[DEBUG] DataFrame data:\n{df}")

        # Check for missing columns
        required_columns = ['Revenue Growth (%)', 'Net Profit Margin (%)', 'ROE (%)',
                          'Debt-to-Equity', 'Free Cash Flow (M)', 'EPS', 'P/E Ratio', 'Current Ratio']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            raise ValueError(f"Missing required columns: {missing_columns}")

        positive = ['Revenue Growth (%)', 'Net Profit Margin (%)', 'ROE (%)',
                    'Free Cash Flow (M)', 'EPS', 'Current Ratio']
        negative = ['Debt-to-Equity', 'P/E Ratio']

        # Calculate scores for each metric
        for col in positive:
            if col in df.columns:
                min_val = df[col].min()
                max_val = df[col].max()
                if max_val == min_val:
                    df[col + '_score'] = 0.5  # If all values are the same, give middle score
                else:
                    df[col + '_score'] = (df[col] - min_val) / (max_val - min_val)
                print(f"[DEBUG] {col}_score calculation: min={min_val}, max={max_val}")

        for col in negative:
            if col in df.columns:
                min_val = df[col].min()
                max_val = df[col].max()
                if max_val == min_val:
                    df[col + '_score'] = 0.5
                else:
                    df[col + '_score'] = (max_val - df[col]) / (max_val - min_val)
                print(f"[DEBUG] {col}_score calculation: min={min_val}, max={max_val}")

        weights = {
            'Revenue Growth (%)_score': 0.2,
            'Net Profit Margin (%)_score': 0.15,
            'ROE (%)_score': 0.2,
            'Debt-to-Equity_score': 0.15,
            'Free Cash Flow (M)_score': 0.1,
            'EPS_score': 0.05,
            'P/E Ratio_score': 0.1,
            'Current Ratio_score': 0.05
        }

        # Check if we have all required score columns
        missing_score_columns = [col for col in weights if col not in df.columns]
        if missing_score_columns:
            raise ValueError(f"Missing score columns: {missing_score_columns}")

        # Calculate final score
        df['Financial Score'] = df[[col for col in weights if col in df.columns]].mul(
            pd.Series(weights), axis=1).sum(axis=1)

        print(f"[DEBUG] Final scores:\n{df[['Financial Score']]}")
        return round(df.iloc[-1]['Financial Score'], 4)

    except Exception as e:
        print(f"\n[ERROR] Detailed error in calculate_financial_score:")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {str(e)}")
        print(f"Company data type: {type(company_data)}")
        print(f"Sector data type: {type(sector_data)}")
        if isinstance(company_data, dict):
            print(f"Company data keys: {company_data.keys()}")
        if isinstance(sector_data, list) and sector_data:
            print(f"First peer data keys: {sector_data[0].keys() if isinstance(sector_data[0], dict) else 'Not a dict'}")
        log_error("calculation", "Error in calculate_financial_score", {
            "error": str(e),
            "error_type": type(e).__name__,
            "company_data": str(company_data),
            "sector_data": str(sector_data)
        })
        raise


# -----------------------------
# 6. Main runner
# -----------------------------
def run_financial_analysis(symbol: str, peer_limit=5):
    print(f"\nAnalyzing {symbol}...")

    try:
        if not is_valid_symbol(symbol):
            print(f"[ERROR] Symbol {symbol} is invalid.")
            log_error("validation", f"Symbol {symbol} is invalid", {"symbol": symbol})
            return None

        print(f"[DEBUG] Fetching company data for {symbol}")
        company_data = fetch_main_company_financials(symbol)
        if not company_data:
            print(f"[ERROR] No data for {symbol}.")
            log_error("data_fetch", f"No data for {symbol}", {"symbol": symbol})
            return None
        print(f"[DEBUG] Company data: {company_data}")

        print(f"[DEBUG] Getting sector peers for {symbol}")
        peers = get_sector_peers(symbol, peer_limit)
        print(f"[DEBUG] Sector Peers: {peers}")

        if not peers:
            print(f"[ERROR] No peers found for {symbol}")
            log_error("data_fetch", f"No peers found for {symbol}", {"symbol": symbol})
            return None

        print(f"[DEBUG] Fetching peer data")
        peer_data = fetch_peer_financials_batch(peers)
        if not peer_data:
            print("[ERROR] Could not fetch peer data.")
            log_error("data_fetch", "Could not fetch peer data", {"symbol": symbol, "peers": peers})
            return None
        print(f"[DEBUG] Peer data: {peer_data}")

        print(f"[DEBUG] Copying company data to peers")
        for peer in peer_data:
            peer['Revenue Growth (%)'] = company_data['Revenue Growth (%)']
            peer['Free Cash Flow (M)'] = company_data['Free Cash Flow (M)']

        print(f"[DEBUG] Calculating financial score")
        score = calculate_financial_score(company_data, peer_data)
        print(f"\nFinal Financial Score for {symbol}: {score}")
        return score
    except Exception as e:
        print(f"[ERROR] Detailed error in run_financial_analysis: {str(e)}")
        print(f"[ERROR] Error type: {type(e).__name__}")
        print(f"[ERROR] Error traceback: {e.__traceback__}")
        log_error("analysis", f"Error in run_financial_analysis for {symbol}", {
            "symbol": symbol,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": str(e.__traceback__)
        })
        return None


# -----------------------------
# 7. Run it
# -----------------------------
if __name__ == "__main__":
    run_financial_analysis("GOOG")

