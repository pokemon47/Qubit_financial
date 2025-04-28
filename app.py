from flask import Flask, jsonify, request
from flask_cors import CORS
from functions_NEW import run_financial_analysis, log_error, fetch_main_company_financials
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
PORT = int(os.getenv('PORT', 5000))

# Define allowed origins
allowed_origins = [
    "http://localhost:3000",                   # local dev
    "https://your-frontend.vercel.app"         # TODO WHEN WE DEPLOY, replace with your deployed frontend domain
]

# Enable CORS for allowed origins only
CORS(app, origins=allowed_origins)

@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "status": "Server is running",
        "port": PORT
    }), 200

@app.route('/financial-score', methods=['GET'])
def get_financial_score():
    ticker = request.args.get('ticker')
    
    if not ticker:
        print("[ERROR] Missing ticker parameter in request")
        log_error("api", "Missing ticker parameter in request")
        return jsonify({
            "error": "Ticker parameter is required"
        }), 400
    
    try:
        # Get company financials
        financials = fetch_main_company_financials(ticker)
        if financials is None:
            print(f"[ERROR] Could not fetch financial data for {ticker}")
            log_error("api", f"Could not fetch financial data for {ticker}", {"ticker": ticker})
            return jsonify({
                "error": f"Could not fetch financial data for {ticker}"
            }), 404

        # Run the financial analysis
        score = run_financial_analysis(ticker)
        if score is None:
            print(f"[ERROR] Could not calculate financial score for {ticker}")
            log_error("api", f"Could not calculate financial score for {ticker}", {"ticker": ticker})
            return jsonify({
                "error": f"Could not calculate financial score for {ticker}"
            }), 404
            
        return jsonify({
            "ticker": ticker,
            "score": score,
            "revenue_growth": financials['Revenue Growth (%)'],
            "net_profit_margin": financials['Net Profit Margin (%)'],
            "free_cash_flow": financials['Free Cash Flow (M)']
        }), 200
    except Exception as e:
        print(f"[ERROR] Unexpected error processing request for {ticker}: {str(e)}")
        log_error("api", f"Unexpected error processing request for {ticker}", {
            "ticker": ticker,
            "error": str(e)
        })
        return jsonify({
            "error": str(e)
        }), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)
