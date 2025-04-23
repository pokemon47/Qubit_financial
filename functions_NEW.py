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
        def fetch():
            quote_resp = get_or_fetch("quotes", {"symbol": symbol}, lambda: requests.get(f"{base_url}/quote/{symbol}?apikey={API_KEY}").json(), ttl_seconds=3600)
            ratio_resp = get_or_fetch("ratios", {"symbol": symbol}, lambda: requests.get(f"{base_url}/ratios-ttm/{symbol}?apikey={API_KEY}").json(), ttl_seconds=86400)

            if not quote_resp or not ratio_resp:
                return None

            quote = quote_resp[0]
            ratio = ratio_resp[0]

            return {
                'Revenue Growth (%)': None,
                'Net Profit Margin (%)': ratio.get('netProfitMarginTTM') * 100 if ratio.get('netProfitMarginTTM') else None,
                'ROE (%)': ratio.get('returnOnEquityTTM') * 100 if ratio.get('returnOnEquityTTM') else None,
                'Debt-to-Equity': ratio.get('debtEquityRatioTTM'),
                'Free Cash Flow (M)': None,
                'EPS': quote.get('eps'),
                'P/E Ratio': quote.get('pe'),
                'Current Ratio': ratio.get('currentRatioTTM')
            }

        # peer_data = get_or_fetch("peer_financials", {"symbol": symbol}, fetch)
        peer_data = get_or_fetch("peer_financials", {"symbol": symbol}, fetch, ttl_seconds=86400)
        if peer_data and all(value is not None for k, value in peer_data.items() if k not in ['Revenue Growth (%)', 'Free Cash Flow (M)']):
            data.append(peer_data)
        else:
            print(f"[WARN] Incomplete or missing data for peer {symbol}, skipping.")

    return data


# -----------------------------
# 5. Score calculation
# -----------------------------
def calculate_financial_score(company_data: dict, sector_data: list) -> float:
    all_data = sector_data + [company_data]
    df = pd.DataFrame(all_data)

    positive = ['Revenue Growth (%)', 'Net Profit Margin (%)', 'ROE (%)',
                'Free Cash Flow (M)', 'EPS', 'Current Ratio']
    negative = ['Debt-to-Equity', 'P/E Ratio']

    for col in positive:
        if col in df.columns:
            df[col + '_score'] = (df[col] - df[col].min()) / (df[col].max() - df[col].min())
    for col in negative:
        if col in df.columns:
            df[col + '_score'] = (df[col].max() - df[col]) / (df[col].max() - df[col].min())

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

    df['Financial Score'] = df[[col for col in weights if col in df.columns]].mul(
        pd.Series(weights), axis=1).sum(axis=1)

    return round(df.iloc[-1]['Financial Score'], 4)


# -----------------------------
# 6. Main runner
# -----------------------------
def run_financial_analysis(symbol: str, peer_limit=5):
    print(f"\nAnalyzing {symbol}...")

    if not is_valid_symbol(symbol):
        print(f"[ERROR] Symbol {symbol} is invalid.")
        return

    company_data = fetch_main_company_financials(symbol)
    if not company_data:
        print(f"[ERROR] No data for {symbol}.")
        return

    peers = get_sector_peers(symbol, peer_limit)
    print(f"Sector Peers: {peers}")

    peer_data = fetch_peer_financials_batch(peers)
    if not peer_data:
        print("[ERROR] Could not fetch peer data.")
        return

    for peer in peer_data:
        peer['Revenue Growth (%)'] = company_data['Revenue Growth (%)']
        peer['Free Cash Flow (M)'] = company_data['Free Cash Flow (M)']

    score = calculate_financial_score(company_data, peer_data)
    print(f"\nFinal Financial Score for {symbol}: {score}")


# -----------------------------
# 7. Run it
# -----------------------------
if __name__ == "__main__":
    run_financial_analysis("GOOG")

