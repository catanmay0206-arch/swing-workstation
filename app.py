import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import os
import time
import io
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False

# --- PAGE CONFIG ---
st.set_page_config(page_title="V-Scout — Stock Analyzer", page_icon="📈", layout="centered")

IST = timezone(timedelta(hours=5, minutes=30))
HOLDINGS_FILE = os.path.join(os.path.dirname(__file__), "data", "holdings.csv")


# --- TICKER RESOLUTION ---
@st.cache_data(ttl=3600)
def resolve_ticker(query):
    q = query.strip()
    if q.upper().endswith((".NS", ".BO")):
        return q.upper()
    try:
        s = yf.Search(q, max_results=6)
        candidates = [r.get("symbol", "") for r in getattr(s, "quotes", [])]
        for sym in candidates:
            if sym.endswith(".NS"):
                return sym
        for sym in candidates:
            if sym.endswith(".BO"):
                return sym
    except Exception:
        pass
    return q.upper().replace(" ", "") + ".NS"


# --- LIVE DATA FETCHING (no synthetic fallback — failures return None) ---
@st.cache_data(ttl=900)
def fetch_history(ticker, period="15mo"):
    try:
        df = yf.Ticker(ticker).history(period=period)
        if df is not None and not df.empty and len(df) >= 20:
            return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        pass
    return None


@st.cache_data(ttl=86400)  # fundamentals barely move intraday — cache a full day, hit Yahoo far less
def fetch_fundamentals(ticker):
    last_error = None
    try:
        if _CFFI_AVAILABLE:
            session = cffi_requests.Session(impersonate="chrome110")
            info = yf.Ticker(ticker, session=session).info
        else:
            info = yf.Ticker(ticker).info
        if info and (info.get("shortName") or info.get("longName") or info.get("trailingPE") or info.get("returnOnEquity")):
            return {
                "name": info.get("shortName") or info.get("longName") or ticker,
                "sector": info.get("sector", "N/A"),
                "pe": info.get("trailingPE"),
                "roe": info.get("returnOnEquity"),
                "de": info.get("debtToEquity"),
                "rev_growth": info.get("revenueGrowth"),
                "_source": "Yahoo Finance",
            }
        last_error = f"Yahoo response empty (keys: {len(info) if info else 0})"
    except Exception as e:
        last_error = f"Yahoo {type(e).__name__}: {e}"

    # Fallback 1: Screener.in's public company page — no login wall for viewing,
    # richer data than NSE (P/E, ROE, ROCE, revenue growth), different provider
    # entirely so it isn't affected by Yahoo's rate limit.
    scr_data, scr_error = fetch_screener_fundamentals(ticker)
    if scr_data:
        return scr_data

    # Fallback 2: NSE India's own quote API — yet another provider/rate-limit
    # bucket. Gives fewer ratios (mainly P/E) but real, live, and free.
    nse_data, nse_error = fetch_nse_fundamentals(ticker)
    if nse_data:
        return nse_data

    cffi_status = "available" if _CFFI_AVAILABLE else "NOT installed (import failed)"
    return {"_error": True, "_detail": f"{last_error} | Screener: {scr_error} | NSE: {nse_error} | curl_cffi: {cffi_status}"}


def fetch_screener_fundamentals(ticker):
    symbol = ticker.replace(".NS", "").replace(".BO", "")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    for url in (f"https://www.screener.in/company/{symbol}/consolidated/", f"https://www.screener.in/company/{symbol}/"):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                continue
            html = resp.text
            soup = BeautifulSoup(html, "html.parser")

            name_tag = soup.find("h1")
            name = name_tag.get_text(strip=True) if name_tag else symbol

            ratios = {}
            top_ratios = soup.find("ul", id="top-ratios")
            if top_ratios:
                for li in top_ratios.find_all("li"):
                    name_span = li.find("span", class_="name")
                    val_span = li.find("span", class_="value")
                    if name_span and val_span:
                        ratios[name_span.get_text(strip=True).lower()] = val_span.get_text(strip=True)

            def parse_num(s):
                if not s:
                    return None
                m = re.search(r"-?[\d,]+\.?\d*", s.replace(",", ""))
                return float(m.group()) if m else None

            pe = parse_num(ratios.get("stock p/e") or ratios.get("price to earning"))
            roe = parse_num(ratios.get("roe"))
            roce = parse_num(ratios.get("roce"))
            de = parse_num(ratios.get("debt to equity"))

            rev_growth = None
            try:
                for df in pd.read_html(io.StringIO(html)):
                    if df.shape[1] == 2 and df.iloc[:, 0].astype(str).str.contains("3 Years", na=False).any():
                        row = df[df.iloc[:, 0].astype(str).str.contains("3 Years", na=False)]
                        rev_growth = parse_num(str(row.iloc[0, 1]))
                        break  # first match in document order is Compounded Sales Growth
            except Exception:
                pass

            if pe or roe or roce:
                return {
                    "name": name, "sector": "N/A",
                    "pe": pe, "roe": (roe or roce) / 100 if (roe or roce) else None,
                    "de": de, "rev_growth": (rev_growth / 100) if rev_growth is not None else None,
                    "_source": "Screener.in",
                }, None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return None, "No usable ratios found on page"


def fetch_nse_fundamentals(ticker):
    symbol = ticker.replace(".NS", "").replace(".BO", "")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=" + symbol,
            "Connection": "keep-alive",
        }
        sess = requests.Session()
        sess.headers.update(headers)
        sess.get("https://www.nseindia.com", timeout=10)  # picks up required cookies
        sess.get(f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}", timeout=10)  # look like a real visit
        resp = sess.get(f"https://www.nseindia.com/api/quote-equity?symbol={symbol}", timeout=10)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        data = resp.json()
        meta = data.get("metadata", {}) or {}
        info = data.get("info", {}) or {}
        pe = meta.get("pdSymbolPe")
        if pe is None and info.get("companyName") is None:
            return None, "No usable fields in response"
        return {
            "name": info.get("companyName", symbol),
            "sector": info.get("industry", "N/A"),
            "pe": pe,
            "roe": None,
            "de": None,
            "rev_growth": None,
            "_source": "NSE India (limited ratios)",
        }, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def compute_rsi(close_series, window=14):
    try:
        return float(RSIIndicator(close_series, window=window).rsi().iloc[-1])
    except Exception:
        return None


def compute_atr_pct(df, window=14):
    try:
        atr = AverageTrueRange(df["High"], df["Low"], df["Close"], window=window).average_true_range()
        last_atr = float(atr.iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        return round((last_atr / last_close) * 100, 2) if last_close else None
    except Exception:
        return None


# --- TECHNICAL LEG (real, computed) ---
def analyze_technical(df):
    close = df["Close"]
    price = float(close.iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    ema150 = float(close.ewm(span=150, adjust=False).mean().iloc[-1]) if len(close) >= 150 else None
    ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1]) if len(close) >= 200 else None
    rsi = compute_rsi(close)
    atr_pct = compute_atr_pct(df)

    score = 50
    points = []
    if price > ema50:
        score += 15
        points.append(f"Price ₹{price:.0f} above 50-EMA ₹{ema50:.0f} — short-term uptrend")
    else:
        score -= 15
        points.append(f"Price ₹{price:.0f} below 50-EMA ₹{ema50:.0f} — short-term downtrend")

    if ema200:
        if price > ema200:
            score += 15
            points.append(f"Price above 200-EMA ₹{ema200:.0f} — long-term trend intact")
        else:
            score -= 15
            points.append(f"Price below 200-EMA ₹{ema200:.0f} — long-term trend weak")

    if rsi is not None:
        if 45 <= rsi <= 70:
            score += 10
            points.append(f"RSI(14) {rsi:.0f} — healthy momentum")
        elif rsi > 80:
            score -= 10
            points.append(f"RSI(14) {rsi:.0f} — overbought, pullback risk")
        elif rsi < 35:
            score -= 10
            points.append(f"RSI(14) {rsi:.0f} — weak momentum")
        elif rsi > 70:
            points.append(f"RSI(14) {rsi:.0f} — firm momentum, watch for overheating")
        else:
            points.append(f"RSI(14) {rsi:.0f} — flat/consolidating momentum")

    if len(points) < 3 and atr_pct is not None:
        points.append(f"ATR(14) {atr_pct:.1f}% of price — {'high' if atr_pct > 5 else 'moderate' if atr_pct > 2 else 'low'} daily volatility")

    score = max(0, min(100, score))

    # Full stack matches V-Momentum's trend gate: price > 50-EMA > 150-EMA > 200-EMA.
    full_stack = bool(ema150 and ema200 and price > ema50 > ema150 > ema200)
    if ema150 and ema200:
        if full_stack:
            trend = "Full uptrend stack (50>150>200)"
        elif price > ema200:
            trend = "Long-term up, short-term pullback"
        else:
            trend = "Below long-term trend"
        points.append(f"50-EMA {'>' if ema50 > ema150 else '<'} 150-EMA {'>' if ema150 > ema200 else '<'} 200-EMA — {'stacked' if full_stack else 'not fully stacked'}")
    else:
        trend = "Limited history"

    return {
        "score": score, "points": points[:4], "price": round(price, 2),
        "ema50": round(ema50, 2), "ema150": round(ema150, 2) if ema150 else None,
        "ema200": round(ema200, 2) if ema200 else None,
        "rsi": round(rsi, 1) if rsi is not None else None, "atr_pct": atr_pct,
        "trend": trend, "full_stack": full_stack,
    }


# --- FUNDAMENTAL LEG (real, computed from yfinance ratios) ---
def analyze_fundamental(f):
    if not f:
        return None
    if f.get("_error"):
        return None
    score = 50
    points = []
    pe = f.get("pe")
    if pe:
        if pe < 20:
            score += 12
            points.append(f"P/E {pe:.1f} — reasonably valued")
        elif pe < 40:
            points.append(f"P/E {pe:.1f} — moderately rich valuation")
        else:
            score -= 12
            points.append(f"P/E {pe:.1f} — expensive vs typical range")
    roe = f.get("roe")
    if roe is not None:
        roe_pct = roe * 100
        if roe_pct >= 15:
            score += 13
            points.append(f"ROE {roe_pct:.1f}% — strong capital efficiency")
        elif roe_pct >= 8:
            points.append(f"ROE {roe_pct:.1f}% — adequate returns")
        else:
            score -= 13
            points.append(f"ROE {roe_pct:.1f}% — weak returns on equity")
    de = f.get("de")
    if de is not None:
        if de < 50:
            score += 12
            points.append(f"Debt/Equity {de:.0f} — low leverage")
        elif de < 150:
            points.append(f"Debt/Equity {de:.0f} — moderate leverage")
        else:
            score -= 12
            points.append(f"Debt/Equity {de:.0f} — high leverage risk")
    rg = f.get("rev_growth")
    if rg is not None:
        rg_pct = rg * 100
        if rg_pct >= 15:
            score += 13
            points.append(f"Revenue growth {rg_pct:.1f}% YoY — expanding fast")
        elif rg_pct >= 0:
            points.append(f"Revenue growth {rg_pct:.1f}% YoY — modest growth")
        else:
            score -= 13
            points.append(f"Revenue growth {rg_pct:.1f}% YoY — contracting")
    if not points:
        return None
    score = max(0, min(100, score))
    return {"score": score, "points": points[:3]}


# --- SENTIMENT LEG (fully free: multi-source news RSS + keyword scoring) ---
# No API key, no cost. This is still keyword-counting, not real reasoning — but
# RESOLUTION_WORDS below patches the single biggest failure mode: a headline like
# "Lawsuit against X dismissed" now reads as resolved instead of scoring negative.
POSITIVE_WORDS = [
    "profit", "growth", "surge", "rally", "upgrade", "beats", "beat estimates",
    "record", "expansion", "strong", "gain", "outperform", "bullish", "robust",
    "jump", "rise", "soar", "wins", "order win", "buyback", "dividend hike",
]
NEGATIVE_WORDS = [
    "loss", "decline", "downgrade", "misses", "miss estimates", "probe", "fraud",
    "lawsuit", "scam", "fall", "plunge", "weak", "bearish", "default", "penalty",
    "crash", "sell-off", "concern", "raid", "resignation", "delay",
]
# If any of these appear in the same headline as a negative word, treat that
# headline's negative hits as resolved (neutral) instead of counting them down.
RESOLUTION_WORDS = [
    "dismissed", "cleared", "settled", "withdrawn", "resolved", "acquitted",
    "closed the case", "dropped", "quashed", "exonerated",
]

NEWS_SOURCES = [
    ("Google News", "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"),
    ("Bing News", "https://www.bing.com/news/search?q={q}&format=RSS"),
]


@st.cache_data(ttl=1800)
def fetch_news_headlines(company, max_items=10):
    queries = [f"{company} share OR stock NSE", f"{company} results", f"{company} news"]
    seen_titles = set()
    items = []
    for name, url_tpl in NEWS_SOURCES:
        for q in queries:
            url = url_tpl.format(q=quote(q))
            try:
                resp = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:max_items]:
                    title_raw = item.findtext("title") or ""
                    pub = item.findtext("pubDate") or ""
                    if " - " in title_raw:
                        title, source = title_raw.rsplit(" - ", 1)
                    else:
                        title, source = title_raw, name
                    title = title.strip()
                    key = title.lower()[:60]
                    if not title or key in seen_titles:
                        continue
                    seen_titles.add(key)
                    try:
                        dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
                        date_str = dt.strftime("%d %b %Y")
                    except Exception:
                        date_str = pub[:16]
                    items.append({"title": title, "source": source.strip() or name, "date": date_str})
            except Exception:
                continue
        if len(items) >= max_items * 2:
            break
    return items[:max_items * 2]


def analyze_sentiment_free(company):
    headlines = fetch_news_headlines(company)
    if not headlines:
        return None, "Could not fetch recent news headlines right now."

    net = 0
    scored = []
    for h in headlines:
        t = h["title"].lower()
        pos_hits = sum(1 for w in POSITIVE_WORDS if w in t)
        neg_hits = sum(1 for w in NEGATIVE_WORDS if w in t)
        if neg_hits and any(w in t for w in RESOLUTION_WORDS):
            pos_hits += neg_hits  # matter resolved — flip from negative to positive
            neg_hits = 0
        tone = pos_hits - neg_hits
        net += tone
        scored.append((tone, h))

    score = max(0, min(100, round(50 + net * 5)))
    scored.sort(key=lambda x: abs(x[0]), reverse=True)

    points = []
    for tone, h in scored[:3]:
        tag = "🟢" if tone > 0 else ("🔴" if tone < 0 else "⚪")
        points.append(f"{tag} {h['title']} ({h['date']})")
    if not points:
        points = [f"⚪ {h['title']} ({h['date']})" for h in headlines[:3]]

    return {"score": score, "points": points}, None


def compute_verdict(fund, tech, sent, owned):
    scores = [s["score"] for s in [fund, tech, sent] if s]
    if not scores:
        return None, None
    composite = round(sum(scores) / len(scores), 1)
    trend_broken = tech and tech.get("price") is not None and tech.get("ema50") is not None and tech["price"] < tech["ema50"]
    if owned:
        verdict = "SELL" if (trend_broken or composite < 45) else "HOLD"
    else:
        # Hard gate: no BUY unless the full EMA stack holds (price > 50 > 150 >
        # 200), matching V-Momentum's stricter trend gate — a short-term dip
        # within a long-term uptrend no longer slips through on sentiment alone.
        full_stack = bool(tech and tech.get("full_stack"))
        verdict = "BUY" if (composite >= 55 and full_stack) else "AVOID"
    return composite, verdict


def holding_hint(tech):
    if not tech:
        return "N/A", "No live data"
    if tech.get("price") is not None and tech.get("ema50") is not None and tech["price"] < tech["ema50"]:
        return "SELL", "Price broke below 50-EMA support"
    if tech.get("rsi") is not None and tech["rsi"] >= 80:
        return "SELL", "RSI overbought (>80) — consider booking profit"
    return "HOLD", "Trend intact, momentum healthy"


# --- HOLDINGS PERSISTENCE ---
def load_holdings():
    if os.path.exists(HOLDINGS_FILE):
        try:
            return pd.read_csv(HOLDINGS_FILE)
        except Exception:
            pass
    return pd.DataFrame([{"Symbol": "", "Buy Price": 0.0, "Qty": 0.0}])


def save_holdings(df):
    os.makedirs(os.path.dirname(HOLDINGS_FILE), exist_ok=True)
    df.to_csv(HOLDINGS_FILE, index=False)


# --- UI ---
st.title("📈 V·Scout")
st.caption("Search a stock for a buy/avoid or hold/sell read, or manage your holdings. 100% free data sources. Research tool only — not investment advice.")

tab_search, tab_holdings = st.tabs(["🔍 Search & Analyze", "📁 My Holdings"])

# --- TAB 1: SEARCH & ANALYZE ---
with tab_search:
    name_input = st.text_input("Company name or NSE ticker", placeholder="e.g. Titagarh Rail Systems or TITAGARH")
    owned = st.checkbox("I already own this")
    entry_price = None
    if owned:
        entry_price = st.number_input("Your entry price (₹)", min_value=0.0, value=0.0, step=1.0)

    if st.button("Analyze", type="primary") and name_input.strip():
        with st.spinner("Resolving ticker and pulling live price data…"):
            ticker = resolve_ticker(name_input)
            hist = fetch_history(ticker)

        if hist is None:
            st.error(f"Could not fetch live price data for '{name_input}' (tried {ticker}). Check spelling, or try the exact NSE ticker (e.g. TITAGARH).")
        else:
            tech = analyze_technical(hist)
            display_name = name_input
            manual_key = f"manual_fund_{ticker}"

            with st.spinner("Pulling fundamentals and scanning recent news…"):
                if manual_key in st.session_state:
                    fund_raw = st.session_state[manual_key]
                    fund_err = None
                else:
                    fund_raw = fetch_fundamentals(ticker)
                    fund_err = fund_raw.get("_detail") if (fund_raw and fund_raw.get("_error")) else None
                fund = analyze_fundamental(fund_raw)
                if fund_raw and fund_raw.get("name"):
                    display_name = fund_raw["name"]
                sent, sent_err = analyze_sentiment_free(display_name)

            composite, verdict = compute_verdict(fund, tech, sent, owned)

            st.subheader(f"{display_name} · {ticker}")
            st.caption(f"Last close ₹{tech['price']} · as of {datetime.now(IST).strftime('%d %b %Y')}")

            if verdict:
                emoji = "🟢" if verdict in ("BUY", "HOLD") else "🔴"
                st.markdown(f"## {emoji} {verdict}" + (f"  ·  composite {composite}/100" if composite else ""))
                if verdict == "AVOID" and composite and composite >= 55 and tech and not tech.get("full_stack"):
                    st.caption("Composite alone would read BUY, but the EMA stack (50>150>200) isn't fully aligned — trend gate blocks new entries until it is, regardless of sentiment/fundamentals.")
            else:
                st.warning("Not enough data to form a verdict.")

            if owned and entry_price:
                pnl = ((tech["price"] - entry_price) / entry_price) * 100
                st.metric("Unrealised P/L", f"{pnl:+.1f}%", f"Entry ₹{entry_price:.2f} → Now ₹{tech['price']:.2f}")

            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown("**Fundamental**")
                if fund:
                    st.progress(fund["score"] / 100)
                    st.caption(f"{fund['score']}/100 · via {fund_raw.get('_source', 'Yahoo Finance')}")
                    for p in fund["points"]:
                        st.write("• " + p)
                    if fund_raw.get("_source") == "manual entry" and st.button("Clear manual entry", key=f"m_clear_{ticker}"):
                        del st.session_state[manual_key]
                        st.rerun()
                else:
                    st.write("Data unavailable")
                    if fund_err:
                        st.caption(f"Debug: {fund_err}")
                    with st.expander("Enter manually instead"):
                        st.caption("Look these up once (e.g. moneycontrol/screener.in) — reused automatically until you clear it.")
                        m_pe = st.number_input("P/E", min_value=0.0, value=0.0, step=0.5, key=f"m_pe_{ticker}")
                        m_roe = st.number_input("ROE %", min_value=0.0, value=0.0, step=0.5, key=f"m_roe_{ticker}")
                        m_de = st.number_input("Debt/Equity", min_value=0.0, value=0.0, step=1.0, key=f"m_de_{ticker}")
                        m_rg = st.number_input("Revenue growth % YoY", value=0.0, step=0.5, key=f"m_rg_{ticker}")
                        if st.button("Save & use these", key=f"m_save_{ticker}"):
                            st.session_state[manual_key] = {
                                "name": display_name, "sector": "N/A",
                                "pe": m_pe or None, "roe": (m_roe / 100) if m_roe else None,
                                "de": m_de or None, "rev_growth": (m_rg / 100) if m_rg else None,
                                "_source": "manual entry",
                            }
                            st.rerun()
            with c2:
                st.markdown("**Technical**")
                st.progress(tech["score"] / 100)
                st.caption(f"{tech['score']}/100 · {tech['trend']}")
                for p in tech["points"]:
                    st.write("• " + p)
            with c3:
                st.markdown("**Sentiment**")
                if sent:
                    st.progress(sent.get("score", 0) / 100)
                    st.caption(f"{sent.get('score', 0)}/100 · keyword-based")
                    for p in sent.get("points", []):
                        st.write("• " + p)
                    st.caption("⚠️ Headline keyword count, not real reasoning — e.g. won't know if a lawsuit was later dismissed.")
                else:
                    st.write(sent_err or "Unavailable")

            st.caption("Research/practice tool only — not investment advice. Verify independently before acting.")

# --- TAB 2: MY HOLDINGS ---
with tab_holdings:
    st.subheader("My Holdings")
    st.caption("Symbol column accepts either the exact NSE ticker (e.g. TATASTEEL, HDFCBANK) or the company name (e.g. Tata Steel) — ticker is more reliable. Current price, unrealised P/L, and the hold/sell hint are pulled live.")

    if "my_holdings" not in st.session_state:
        st.session_state["my_holdings"] = load_holdings()

    edited = st.data_editor(st.session_state["my_holdings"], num_rows="dynamic", use_container_width=True, key="holdings_editor")
    st.session_state["my_holdings"] = edited
    save_holdings(edited)

    rows = []
    for _, row in edited.iterrows():
        sym = str(row.get("Symbol", "")).strip()
        if not sym:
            continue
        ticker = resolve_ticker(sym)
        try:
            buy_p = float(row.get("Buy Price", 0) or 0)
        except (ValueError, TypeError):
            buy_p = 0.0
        try:
            qty = float(row.get("Qty", 0) or 0)
        except (ValueError, TypeError):
            qty = 0.0

        hist = fetch_history(ticker)
        if hist is None or buy_p <= 0:
            rows.append({
                "Symbol": sym, "Resolved As": ticker, "Buy Price": buy_p, "Qty": qty if qty else "-",
                "Current Price": "N/A", "Unrealised P/L": "N/A",
                "Hint": "⚠️ No data", "Reason": "Could not fetch live price — check symbol/name",
            })
            continue

        tech = analyze_technical(hist)
        pnl_pct = ((tech["price"] - buy_p) / buy_p) * 100
        pnl_abs = (tech["price"] - buy_p) * qty if qty else None
        hint, reason = holding_hint(tech)

        rows.append({
            "Symbol": sym, "Resolved As": ticker, "Buy Price": buy_p, "Qty": qty if qty else "-",
            "Current Price": tech["price"],
            "Unrealised P/L": f"{pnl_pct:+.1f}%" + (f" (₹{pnl_abs:+,.0f})" if pnl_abs else ""),
            "Hint": "🟢 HOLD" if hint == "HOLD" else ("🔴 SELL" if hint == "SELL" else hint),
            "Reason": reason,
        })

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        st.caption("Check the 'Resolved As' column — if it matched the wrong stock, retype using the exact NSE ticker instead of the company name.")
    else:
        st.info("Add a symbol and buy price above to see live P/L and a hold/sell hint.")

    st.caption("Holdings are saved on this deployment and persist across refreshes. Prices are cached ~15 minutes.")
