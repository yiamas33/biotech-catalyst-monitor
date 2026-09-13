#!/usr/bin/env python3
"""
Biotech Catalyst Monitor — Weekly Data Puller
Pulls FMP API data + FDA calendar, outputs CSV for Claude scoring

Schedule: Every Friday 09:00 EET via GitHub Actions or local cron
Output: biotech_catalysts_YYYY-MM-DD.csv (push to GitHub / Google Drive)
"""

import os
import sys
import json
import csv
from datetime import datetime, timedelta
import requests
from typing import Dict, List, Optional

# Configuration
FMP_API_KEY = os.getenv('FMP_API_KEY')  # Set this as GitHub Secret or local env var
FMP_BASE = "https://financialmodelingprep.com/api/v3"

# Tickers to monitor (Biotech Catalyst Universe)
MONITORED_TICKERS = [
    'CAPR',  # Capricor — Deramiocel (Nov 22, 2026)
    'NTLA',  # Intellia — Lonvo-z (Mar 10, 2027)
    'AMLX',  # Amylyx — Avexitide (Q2 2027)
    'GPCR',  # Structure — Aleniglipron (H1 2027)
    'INSM', 'ARGX', 'VEEV', 'NTRA', 'ARWR', 'CRSP', 'CDNA',  # Expansion universe
]

# Known PDUFA dates (manually curated; update weekly from FDA calendar)
PDUFA_DATES = {
    'CAPR': '2026-11-22',
    'NTLA': '2027-03-10',
    'AMLX': '2027-06-30',  # Estimated Q2 2027
    'GPCR': None,  # TBD (waiting for Phase 3 data + End-of-Phase-2 meeting)
}

class BiotechCatalystPuller:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'BiotechMonitor/1.0'})
        self.data = []

    def fetch_quote(self, ticker: str) -> Optional[Dict]:
        """Fetch current quote from FMP."""
        try:
            url = f"{FMP_BASE}/quote-short/{ticker}"
            params = {'apikey': self.api_key}
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception as e:
            print(f"⚠️ Error fetching quote {ticker}: {e}", file=sys.stderr)
        return None

    def fetch_historical_prices(self, ticker: str, days: int = 60) -> Optional[List[Dict]]:
        """Fetch last N days of OHLCV data."""
        try:
            url = f"{FMP_BASE}/historical-price-full/{ticker}"
            params = {'apikey': self.api_key, 'limit': days}
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data and 'historical' in data:
                return sorted(data['historical'], key=lambda x: x['date'])
        except Exception as e:
            print(f"⚠️ Error fetching historical {ticker}: {e}", file=sys.stderr)
        return None

    def calculate_moving_averages(self, prices: List[Dict]) -> Dict:
        """Calculate 20D MA, 60D MA, volume averages, 60D return."""
        if not prices or len(prices) < 60:
            return {}

        closes_20d = [p['close'] for p in prices[-20:]]
        closes_60d = [p['close'] for p in prices[-60:]]
        vols_20d = [p['volume'] for p in prices[-20:]]
        vols_60d = [p['volume'] for p in prices[-60:]]

        ma20 = sum(closes_20d) / len(closes_20d) if closes_20d else 0
        ma60 = sum(closes_60d) / len(closes_60d) if closes_60d else 0
        vol20_avg = sum(vols_20d) / len(vols_20d) if vols_20d else 0
        vol60_avg = sum(vols_60d) / len(vols_60d) if vols_60d else 0

        # MA slope (linear regression over last 5 days of 20D MA)
        # Simplified: just check if last 20D MA > average of 20D MA from 10 days ago
        ma20_slope = closes_20d[-1] - closes_20d[0]  # Change over 20 days

        # Up-volume vs down-volume (last 10 days)
        ups = sum([p['volume'] for p in prices[-10:] if p['close'] > p['open']])
        downs = sum([p['volume'] for p in prices[-10:] if p['close'] <= p['open']])

        # 60-day return (from 60 days ago to now)
        price_60d_ago = closes_60d[0]
        price_now = closes_20d[-1]
        return_60d_pct = ((price_now - price_60d_ago) / price_60d_ago * 100) if price_60d_ago > 0 else 0
        
        # Early run detection: has stock already run >30% in past 60 days?
        early_run = return_60d_pct > 30

        return {
            'ma20': round(ma20, 2),
            'ma60': round(ma60, 2),
            'vol20_avg': round(vol20_avg, 0),
            'vol60_avg': round(vol60_avg, 0),
            'ma20_slope': round(ma20_slope, 2),
            'up_volume': round(ups, 0),
            'down_volume': round(downs, 0),
            'up_volume_pct': round(100 * ups / (ups + downs), 1) if (ups + downs) > 0 else 0,
            'return_60d_pct': round(return_60d_pct, 2),
            'early_run_warning': early_run,
        }

    def check_momentum_confirmations(self, ticker: str, quote: Dict, technicals: Dict) -> Dict:
        """Check 4 momentum conditions; return count of confirmations (0-4)."""
        current_price = quote.get('price', 0)
        ma20 = technicals.get('ma20', 0)
        ma20_slope = technicals.get('ma20_slope', 0)
        vol20_avg = technicals.get('vol20_avg', 0)
        vol60_avg = technicals.get('vol60_avg', 0)
        up_pct = technicals.get('up_volume_pct', 0)

        confirmations = 0
        details = []

        # 1. Price > 20D MA
        if current_price > ma20:
            confirmations += 1
            details.append(f"✓ Price {current_price} > MA20 {ma20}")
        else:
            details.append(f"✗ Price {current_price} <= MA20 {ma20}")

        # 2. MA20 uptrend (slope > 0)
        if ma20_slope > 0:
            confirmations += 1
            details.append(f"✓ MA20 slope +{ma20_slope}")
        else:
            details.append(f"✗ MA20 slope {ma20_slope}")

        # 3. Volume > 60D average
        if vol20_avg > vol60_avg * 1.2:  # 20% above 60D avg
            confirmations += 1
            details.append(f"✓ Vol20 {vol20_avg:.0f} > Vol60 {vol60_avg:.0f}")
        else:
            details.append(f"✗ Vol20 {vol20_avg:.0f} <= Vol60 {vol60_avg:.0f}")

        # 4. Up-volume > down-volume
        if up_pct > 50:
            confirmations += 1
            details.append(f"✓ Up-vol {up_pct}% > 50%")
        else:
            details.append(f"✗ Up-vol {up_pct}% <= 50%")

        return {
            'confirmations': confirmations,
            'eligible_3of4': confirmations >= 3,
            'details': ' | '.join(details)
        }

    def calculate_days_to_pdufa(self, pdufa_date: Optional[str]) -> Optional[Dict]:
        """Calculate days until PDUFA, T-60, T-14."""
        if not pdufa_date:
            return None

        try:
            pdufa_dt = datetime.strptime(pdufa_date, '%Y-%m-%d')
            today = datetime.now()
            days_left = (pdufa_dt - today).days

            if days_left < 0:
                status = 'PASSED'
                t60_date = None
                t14_date = None
            else:
                status = 'ACTIVE'
                t60_dt = pdufa_dt - timedelta(days=60)
                t14_dt = pdufa_dt - timedelta(days=14)
                t60_date = t60_dt.strftime('%Y-%m-%d')
                t14_date = t14_dt.strftime('%Y-%m-%d')

                # Is T-60 trigger active (today >= T-60)?
                if today >= t60_dt:
                    status = 'T-60 ACTIVE'

            return {
                'pdufa_date': pdufa_date,
                'days_to_pdufa': days_left,
                'status': status,
                't60_date': t60_date,
                't14_date': t14_date,
                't60_active': (datetime.now() >= (pdufa_dt - timedelta(days=60))) if days_left > 0 else False,
            }
        except Exception as e:
            print(f"⚠️ Error calculating PDUFA dates for {pdufa_date}: {e}", file=sys.stderr)
        return None

    def pull_all_data(self):
        """Main loop: pull quote + technicals + PDUFA for each ticker."""
        for ticker in MONITORED_TICKERS:
            print(f"Pulling {ticker}...", file=sys.stderr)

            # Get current quote
            quote = self.fetch_quote(ticker)
            if not quote:
                print(f"  ⚠️ Skipping {ticker} (quote failed)", file=sys.stderr)
                continue

            # Get historical prices
            prices = self.fetch_historical_prices(ticker, days=60)
            if not prices or len(prices) < 20:
                print(f"  ⚠️ Skipping {ticker} (insufficient history)", file=sys.stderr)
                continue

            # Calculate technicals
            technicals = self.calculate_moving_averages(prices)
            momentum = self.check_momentum_confirmations(ticker, quote, technicals)
            pdufa = self.calculate_days_to_pdufa(PDUFA_DATES.get(ticker))

            # Compile row
            row = {
                'ticker': ticker,
                'date': datetime.now().strftime('%Y-%m-%d'),
                'price': quote.get('price', 'N/A'),
                'price_change_pct': quote.get('changesPercentage', 'N/A'),
                'market_cap': quote.get('marketCap', 'N/A'),
                'avg_volume_3m': quote.get('avgVolume', 'N/A'),
                'ma20': technicals.get('ma20', 'N/A'),
                'ma60': technicals.get('ma60', 'N/A'),
                'vol20_avg': technicals.get('vol20_avg', 'N/A'),
                'vol60_avg': technicals.get('vol60_avg', 'N/A'),
                'ma20_slope': technicals.get('ma20_slope', 'N/A'),
                'up_volume_pct': technicals.get('up_volume_pct', 'N/A'),
                'return_60d_pct': technicals.get('return_60d_pct', 'N/A'),
                'early_run_warning': technicals.get('early_run_warning', False),
                'momentum_confirmations': momentum.get('confirmations', 0),
                'eligible_3of4': momentum.get('eligible_3of4', False),
                'momentum_details': momentum.get('details', ''),
                'pdufa_date': pdufa.get('pdufa_date') if pdufa else 'N/A',
                'days_to_pdufa': pdufa.get('days_to_pdufa') if pdufa else 'N/A',
                'pdufa_status': pdufa.get('status') if pdufa else 'N/A',
                't60_date': pdufa.get('t60_date') if pdufa else 'N/A',
                't14_date': pdufa.get('t14_date') if pdufa else 'N/A',
                't60_active': pdufa.get('t60_active') if pdufa else False,
            }
            self.data.append(row)

        return self.data

    def export_csv(self, filename: Optional[str] = None) -> str:
        """Export data to CSV."""
        if filename is None:
            filename = f"biotech_catalysts_{datetime.now().strftime('%Y-%m-%d')}.csv"

        if not self.data:
            print("⚠️ No data to export", file=sys.stderr)
            return filename

        keys = self.data[0].keys()
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(self.data)

        print(f"✓ Exported {len(self.data)} rows to {filename}", file=sys.stderr)
        return filename


def main():
    if not FMP_API_KEY:
        print("❌ FMP_API_KEY not set. Set as environment variable: export FMP_API_KEY='your_key'", file=sys.stderr)
        sys.exit(1)

    puller = BiotechCatalystPuller(FMP_API_KEY)
    puller.pull_all_data()
    filename = puller.export_csv()
    print(filename)  # Output filename to stdout for GitHub Actions


if __name__ == '__main__':
    main()
