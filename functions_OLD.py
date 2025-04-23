import requests
import pandas as pd
import re
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Load the API_KEY from .env file
API_KEY = os.getenv("FMP_KEY")

# -----------------------------
# 1. Validate stock symbol
# -----------------------------
def is_valid_symbol(symbol: str) -> bool:
    if not re.match(r"^[A-Z\.]{1,6}$", symbol.upper()):
        print(f"[ERROR] Invalid symbol format: {symbol}")
        return False

    url = f"https://financialmodelingprep.com/api/v3/search?query={symbol}&limit=1&apikey={API_KEY}"
    response = requests.get(url).json()
    return bool(response and response[0]['symbol'].upper() == symbol.upper())

# -----------------------------
# 2. Get peers from same industry
# -----------------------------
def get_industry_peers(symbol: str, limit=5) -> list:
    url = f"https://financialmodelingprep.com/api/v3/profile/{symbol}?apikey={API_KEY}"
    profile = requests.get(url).json()
    
    if not profile:
        print("[ERROR] Could not get profile info.")
        return []

    company = profile[0]
    industry = company.get('industry')
    sector = company.get('sector')

    # Try industry first
    if industry:
        screener_url = f"https://financialmodelingprep.com/api/v3/stock-screener?industry={industry}&limit=50&apikey={API_KEY}"
        companies = requests.get(screener_url).json()
        if companies:
            return [c['symbol'] for c in companies if c['symbol'] != symbol][:limit]

    # Fall back to sector
    print(f"[INFO] No peers found using industry '{industry}'. Trying sector '{sector}'...")
    if sector:
        print(sector)
        screener_url = f"https://financialmodelingprep.com/api/v3/stock-screener?sector={sector}&limit=50&apikey={API_KEY}"
        companies = requests.get(screener_url).json()
        if companies:
            return [c['symbol'] for c in companies if c['symbol'] != symbol][:limit]

    print("[ERROR] No peers found using industry or sector.")
    return []

# -----------------------------
# 3. Fetch main company financials
# -----------------------------
def fetch_main_company_financials(symbol: str) -> dict:
    base_url = "https://financialmodelingprep.com/api/v3"
    try:
        income = requests.get(f"{base_url}/income-statement/{symbol}?limit=2&apikey={API_KEY}").json()
        cashflow = requests.get(f"{base_url}/cash-flow-statement/{symbol}?limit=2&apikey={API_KEY}").json()
        ratios = requests.get(f"{base_url}/ratios-ttm/{symbol}?apikey={API_KEY}").json()
        quote = requests.get(f"{base_url}/quote/{symbol}?apikey={API_KEY}").json()

        if not income or not cashflow or not ratios or not quote:
            print(f"[ERROR] Missing data for {symbol}")
            return None

        # Safely get values from the response (using .get() to avoid KeyError)
        revenue_growth = (
            (income[0].get('revenue', 0) - income[1].get('revenue', 0)) / income[1].get('revenue', 1)
        ) * 100 if income and len(income) > 1 else 0
        free_cash_flow = cashflow[0].get('freeCashFlow', 0) / 1_000_000 if cashflow else 0

        return {
            'Revenue Growth (%)': revenue_growth,
            'Net Profit Margin (%)': ratios[0].get('netProfitMarginTTM', 0) * 100 if ratios else 0,
            'ROE (%)': ratios[0].get('returnOnEquityTTM', 0) * 100 if ratios else 0,
            'Debt-to-Equity': ratios[0].get('debtEquityRatioTTM', 0) if ratios else 0,
            'Free Cash Flow (M)': free_cash_flow,
            'EPS': quote[0].get('eps', 0) if quote else 0,
            'P/E Ratio': quote[0].get('pe', 0) if quote else 0,
            'Current Ratio': ratios[0].get('currentRatioTTM', 0) if ratios else 0
        }

    except Exception as e:
        print(f"[ERROR] Failed to fetch data for {symbol}: {e}")
        return None


# -----------------------------
# 4. Fetch peer financials (batched, excluding revenue and FCF)
# -----------------------------
def fetch_peer_financials_batch(symbols: list) -> list:
    base_url = "https://financialmodelingprep.com/api/v3"
    data = []

    for symbol in symbols:
        try:
            quote_resp = requests.get(f"{base_url}/quote/{symbol}?apikey={API_KEY}").json()
            ratio_resp = requests.get(f"{base_url}/ratios-ttm/{symbol}?apikey={API_KEY}").json()

            print(f"[DEBUG] {symbol} Quote: {quote_resp}")
            print(f"[DEBUG] {symbol} Ratios: {ratio_resp}")

            if not quote_resp or not ratio_resp:
                print(f"[WARN] Missing data for peer {symbol}, skipping.")
                continue

            quote = quote_resp[0]
            ratio = ratio_resp[0]

            peer_data = {
                'Revenue Growth (%)': None,  # Will be filled later
                'Net Profit Margin (%)': ratio.get('netProfitMarginTTM') * 100 if ratio.get('netProfitMarginTTM') else None,
                'ROE (%)': ratio.get('returnOnEquityTTM') * 100 if ratio.get('returnOnEquityTTM') else None,
                'Debt-to-Equity': ratio.get('debtEquityRatioTTM'),
                'Free Cash Flow (M)': None,  # Will be filled later
                'EPS': quote.get('eps'),
                'P/E Ratio': quote.get('pe'),
                'Current Ratio': ratio.get('currentRatioTTM')
            }

            # Only include peer if all relevant fields are present (except FCF and Revenue Growth)
            if all(value is not None for key, value in peer_data.items() if key not in ['Free Cash Flow (M)', 'Revenue Growth (%)']):
                data.append(peer_data)
            else:
                print(f"[WARN] Incomplete data for peer {symbol}, skipping.")

        except Exception as e:
            print(f"[ERROR] Failed to fetch data for peer {symbol}: {e}")

    return data


# -----------------------------
# 5. Score calculation
# -----------------------------
def calculate_financial_score(company_data: dict, industry_data: list) -> float:
    all_data = industry_data + [company_data]
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

    peers = get_industry_peers(symbol, peer_limit)
    print(f"Industry Peers: {peers}")

    peer_data = fetch_peer_financials_batch(peers)
    if not peer_data:
        print("[ERROR] Could not fetch peer data.")
        return

    # Fill missing fields for peers (assume industry average or zero for FCF/revenue growth)
    for peer in peer_data:
        peer['Revenue Growth (%)'] = company_data['Revenue Growth (%)']  # fallback
        peer['Free Cash Flow (M)'] = company_data['Free Cash Flow (M)']  # fallback

    score = calculate_financial_score(company_data, peer_data)
    print(f"\nFinal Financial Score for {symbol}: {score}")

# -----------------------------
# 7. Run it
# -----------------------------
if __name__ == "__main__":
    run_financial_analysis("ORCL")
