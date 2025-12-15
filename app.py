import streamlit as st
import yfinance as yf
import pandas as pd
import sqlite3
import plotly.express as px
from datetime import datetime, date, timedelta
import math
import plotly.graph_objects as go
import time

# --- 設定頁面 ---
st.set_page_config(page_title="資產管理系統)", layout="wide", page_icon="📈")

# --- 全域常數 ---
DB_FILE = "investment_data_final.db"
INTEREST_RATE_MARGIN = 0.0625
RISK_FREE_RATE = 0.015

# --- [Optimization] Session State for Cache Control ---
if 'db_updated' not in st.session_state:
    st.session_state.db_updated = time.time()

# --- 資料庫初始化 ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  date TEXT,
                  symbol TEXT,
                  stock_name TEXT,
                  trans_type TEXT, 
                  mode TEXT,       
                  price REAL,
                  qty INTEGER,
                  fee REAL,
                  tax REAL,
                  loan_amount REAL,   
                  interest REAL,      
                  cash_flow REAL,     
                  realized_pl REAL,   
                  cost_basis REAL     
                  )''')
    c.execute('''CREATE TABLE IF NOT EXISTS price_history
                 (symbol TEXT, 
                  date TEXT, 
                  close REAL,
                  PRIMARY KEY (symbol, date))''')
    conn.commit()
    conn.close()

def sync_price_history(symbols):
    if not symbols: return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    today = date.today()
    
    for sym in symbols:
        c.execute("SELECT MAX(date) FROM price_history WHERE symbol = ?", (sym,))
        result = c.fetchone()[0]
        
        start_date = None
        if result:
            last_date = datetime.strptime(result, "%Y-%m-%d").date()
            if last_date >= today: 
                continue 
            start_date = last_date + timedelta(days=1)
        else:
            start_date = date(2023, 1, 1) 

        if start_date <= today:
            print(f"📥 Updating {sym} from {start_date}...") # Debug info
            try:
                # yfinance ensures we get the latest data
                df = yf.download(sym, start=start_date, end=today + timedelta(days=1), progress=False, threads=False)
                
                if not df.empty:
                    # Prepare data for insertion
                    data_to_insert = []
                    for idx, row in df.iterrows():
                        # Handle different yfinance return formats (Series vs DataFrame)
                        val = row['Close'].iloc[0] if isinstance(row['Close'], pd.Series) else row['Close']
                        d_str = idx.strftime('%Y-%m-%d')
                        data_to_insert.append((sym, d_str, float(val)))
                    
                    # 3. Batch insert into SQLite
                    c.executemany("INSERT OR IGNORE INTO price_history (symbol, date, close) VALUES (?, ?, ?)", data_to_insert)
                    conn.commit()
            except Exception as e:
                print(f"Error updating {sym}: {e}")

    conn.close()
    
def trigger_db_update():
    st.session_state.db_updated = time.time()

# --- 寫入交易 ---
def add_transaction(date_str, symbol, name, trans_type, mode, price, qty, fee, tax, loan, interest, calculated_pl=0, cost_basis=0):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # --- 現金流計算 (Cash Flow) ---
    raw_val = price * qty
    cash_flow = 0
    
    if mode == "Spot" or mode == "Day Trade":
        if trans_type == 'Buy':
            cash_flow = -(raw_val + fee)
        else:
            cash_flow = raw_val - fee - tax

    elif mode == "Margin":
        if trans_type == 'Buy':
            own_funds = raw_val - loan
            cash_flow = -(own_funds + fee)
        else:
            cash_flow = raw_val - loan - fee - tax - interest

    elif mode == "Short":
        if trans_type == 'Sell': 
            margin_deposit = loan 
            cash_flow = -(margin_deposit + fee + tax)
        else: 
            total_refund = loan 
            buy_cost = raw_val + fee
            cash_flow = total_refund - buy_cost

    c.execute("""
        INSERT INTO transactions 
        (date, symbol, stock_name, trans_type, mode, price, qty, fee, tax, loan_amount, interest, cash_flow, realized_pl, cost_basis) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date_str, symbol, name, trans_type, mode, price, qty, fee, tax, loan, interest, cash_flow, calculated_pl, cost_basis))
    conn.commit()
    conn.close()
    
    
    trigger_db_update()


@st.cache_data
def get_transactions(last_update_time):
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM transactions ORDER BY date ASC, id ASC", conn)
    conn.close()
    return df

@st.cache_data(ttl=3600)
def fetch_current_prices(symbols):
    if not symbols: return {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        prices = {}
        for sym in symbols:
            try:
                prices[sym] = tickers.tickers[sym].fast_info.last_price
            except:
                prices[sym] = 0
        return prices
    except:
        return {}

@st.cache_data
def calculate_current_holdings(df):
    if df.empty: return [], {}, 0, 0, 0
    
    symbol_name_map = df.groupby('symbol')['stock_name'].last().to_dict()
    holdings_map = {}
    
    # 1. Aggregate Quantities
    for _, row in df.iterrows():
        sym = row['symbol']
        if sym not in holdings_map: 
            holdings_map[sym] = {'qty': 0, 'loan': 0, 'short_qty': 0, 'short_deposit': 0, 'short_collateral': 0}
        
        if row['trans_type'] == 'Buy':
            if row['mode'] == 'Short': # Cover
                holdings_map[sym]['short_qty'] -= row['qty']
                if holdings_map[sym]['short_qty'] <= 0:
                    holdings_map[sym]['short_deposit'] = 0
                    holdings_map[sym]['short_collateral'] = 0
            else: # Buy Long
                holdings_map[sym]['qty'] += row['qty']
                holdings_map[sym]['loan'] += row['loan_amount']
        else: # Sell
            if row['mode'] == 'Short': # Short Open
                holdings_map[sym]['short_qty'] += row['qty']
                holdings_map[sym]['short_deposit'] += row['loan_amount']
                holdings_map[sym]['short_collateral'] += (row['price'] * row['qty'])
            else: # Sell Long
                holdings_map[sym]['qty'] -= row['qty']
                holdings_map[sym]['loan'] -= row['loan_amount']

    # Filter active symbols
    active_syms = [k for k, v in holdings_map.items() if v['qty'] > 0 or v['short_qty'] > 0]
    return active_syms, holdings_map, symbol_name_map

def recalculate_history():
    """
    核心功能：重新計算所有歷史損益 (Replay All Transactions)
    解決補登舊資料導致 FIFO 順序錯誤的問題。
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    
    c.execute("SELECT * FROM transactions ORDER BY date ASC, id ASC")
    rows = c.fetchall()

    inventory = {}
    
    
    updates = []
    
    for r in rows:
        r_id, r_date, sym, name, t_type, mode, price, qty, fee, tax, loan, interest, cash, pl, cost = r
        
        # 確保字典結構存在
        if sym not in inventory: inventory[sym] = {}
        if mode not in inventory[sym]: inventory[sym][mode] = []
        
        current_date = datetime.strptime(r_date, "%Y-%m-%d").date()
        
        # --- CASE A: 建立部位 (買進現股/融資，或 賣出融券) ---
        is_opening = (t_type == 'Buy' and mode != 'Short') or (t_type == 'Sell' and mode == 'Short')
        
        if is_opening:
            lot = {
                'id': r_id,
                'date': current_date,
                'qty': qty,
                'price': price,
                'loan': loan,
                'fee': fee, 
                'orig_cash_flow': abs(cash) 
            }
            inventory[sym][mode].append(lot)
            updates.append((0, 0, interest, cash, r_id)) 


        else:
            qty_to_match = qty
            
            accumulated_cost = 0     
            accumulated_interest = 0 
            repaid_loan = 0           
            
            lots = inventory[sym][mode]
            active_lots = [] 
            for lot in lots:
                if qty_to_match <= 0:
                    active_lots.append(lot) # 已經扣完了，剩下的放回庫存
                    continue
                
                matched_qty = min(lot['qty'], qty_to_match)
                
                # 1. 計算比例成本
                
                if mode == 'Spot':
                    # 現股成本 = (股價*股數 + 手續費)
                    lot_total_cost = (lot['price'] * lot['qty']) + lot['fee']
                    part_cost = (lot_total_cost / lot['qty']) * matched_qty
                    accumulated_cost += part_cost
                    
                elif mode == 'Margin':
                    lot_total_basis = (lot['price'] * lot['qty']) + lot['fee']
                    part_cost = (lot_total_basis / lot['qty']) * matched_qty
                    accumulated_cost += part_cost
                    
                    part_loan = (lot['loan'] / lot['qty']) * matched_qty
                    repaid_loan += part_loan
                    
                    days = (current_date - lot['date']).days
                    if days < 0: days = 0
                    # 融資利息
                    interest_amt = part_loan * INTEREST_RATE_MARGIN * (days / 365)
                    accumulated_interest += interest_amt

                elif mode == 'Short':
                    part_loan = (lot['loan'] / lot['qty']) * matched_qty # 退回保證金
                    repaid_loan += part_loan
                    accumulated_interest += 0 

                qty_to_match -= matched_qty
                
                # 處理剩餘股數
                if lot['qty'] > matched_qty:
                    lot['qty'] -= matched_qty
                    remaining_ratio = lot['qty'] / (lot['qty'] + matched_qty)
                    lot['loan'] *= remaining_ratio
                    lot['fee'] *= remaining_ratio
                    active_lots.append(lot)
            
            # 更新庫存
            inventory[sym][mode] = active_lots
            
            # --- 結算這筆交易的最終數值 ---
            final_interest = int(accumulated_interest)
            final_cost_basis = int(accumulated_cost)
            final_pl = 0
            new_cash_flow = 0
            
            # A. 現股賣出
            if mode == 'Spot':
                # 淨拿回 = (賣價*股數) - 費 - 稅
                net_proceeds = (price * qty) - fee - tax
                final_pl = net_proceeds - final_cost_basis
                new_cash_flow = net_proceeds # 現金流就是拿回多少錢
                
            # B. 融資賣出
            elif mode == 'Margin':
                gross_proceeds = (price * qty)
                new_cash_flow = gross_proceeds - fee - tax - repaid_loan - final_interest
                final_pl = (gross_proceeds - fee - tax - final_interest) - final_cost_basis

            # C. 融券回補
            elif mode == 'Short':
                # 買回支出 = (買價*股數 + 費)
                buy_back_cost = (price * qty) + fee
                final_cost_basis = int(accumulated_cost) # 當初賣出的淨額
                final_pl = final_cost_basis - buy_back_cost
                new_cash_flow = repaid_loan + final_pl # 簡單推算：拿回的錢 = 保證金 + 賺的錢(或賠的錢)

            updates.append((final_pl, final_cost_basis, final_interest, int(new_cash_flow), r_id))

    # 3. 批次寫回資料庫
    c.executemany("""
        UPDATE transactions 
        SET realized_pl=?, cost_basis=?, interest=?, cash_flow=? 
        WHERE id=?
    """, updates)
    
    conn.commit()
    conn.close()
    
    # 記得更新外部 Cache 時間戳記
    trigger_db_update()
    
# --- FIFO Logic (Unchanged but uses cached DF) ---
def calculate_fifo_outcome(symbol, sell_qty, sell_price, sell_date_str, mode):
    # Pass the current timestamp to get the cached DF
    df = get_transactions(st.session_state.db_updated)
    
    open_type = 'Buy' if mode != 'Short' else 'Sell'
    records = df[(df['symbol'] == symbol) & (df['mode'] == mode) & (df['trans_type'] == open_type)].copy()
    
    close_type = 'Sell' if mode != 'Short' else 'Buy'
    closed_records = df[(df['symbol'] == symbol) & (df['mode'] == mode) & (df['trans_type'] == close_type)].copy()
    already_closed_qty = closed_records['qty'].sum() if not closed_records.empty else 0
    
    val_1 = 0 
    val_2 = 0 
    total_cost_basis = 0 
    
    qty_to_match = sell_qty
    current_date = datetime.strptime(str(sell_date_str), "%Y-%m-%d").date()

    for _, row in records.iterrows():
        if qty_to_match <= 0: break
            
        orig_qty = row['qty']
        if already_closed_qty >= orig_qty:
            already_closed_qty -= orig_qty
            continue
        
        available = orig_qty - already_closed_qty
        already_closed_qty = 0 
        
        matched = min(available, qty_to_match)
        
        if mode == 'Margin':
            part_loan = (row['loan_amount'] / row['qty']) * matched
            val_1 += part_loan
            
            buy_date = datetime.strptime(row['date'], "%Y-%m-%d").date()
            days = max(0, (current_date - buy_date).days)
            interest = part_loan * INTEREST_RATE_MARGIN * (days / 365)
            val_2 += interest
            
            orig_cash_out = abs(row['cash_flow']) 
            part_cost = (orig_cash_out / row['qty']) * matched
            total_cost_basis += part_cost

        elif mode == 'Short':
            part_deposit = (row['loan_amount'] / row['qty']) * matched
            val_1 += part_deposit
            
            part_collateral = row['price'] * matched
            val_2 += part_collateral
            
            orig_cash_out = abs(row['cash_flow'])
            part_cost = (orig_cash_out / row['qty']) * matched
            total_cost_basis += part_cost

        elif mode == 'Spot':
            orig_invest = (row['price'] * row['qty']) + row['fee']
            part_cost = (orig_invest / row['qty']) * matched
            total_cost_basis += part_cost

        qty_to_match -= matched

    return int(val_1), int(val_2), int(total_cost_basis)

@st.cache_data
def calculate_portfolio_metrics(df, current_holdings):
    """
    修正版 V3：
    1. [P/L 與 ROI]：基於「自備款 (Own Funds)」計算，反映槓桿獲利能力。
    2. [顯示用的平均成本]：基於「全額成交價 (Full Basis)」計算，反映原始股價位置。
    """
    if not current_holdings:
        return [], 0, 0
        
    enriched_holdings = []
    total_mkt_val = 0
    total_own_funds = 0 
    
    df_sorted = df.sort_values(['date', 'id'])
    trans_group = df_sorted.groupby('symbol')

    for item in current_holdings:
        sym = item['symbol']
        qty_held = item['qty']
        mkt_val = item.get('display_mkt', 0)
        
        if sym in trans_group.groups:
            sym_trans = trans_group.get_group(sym)
        else:
            sym_trans = pd.DataFrame()

        # 初始化兩組累計器
        acc_own_funds = 0       # 累計自備款 (算 ROI 用)
        acc_loan_repay = 0      # 累計需還款 (算 淨值 用)
        acc_full_basis = 0      # 累計全額成本 (顯示 平均成本 用)
        
        # === 邏輯 A: 做多部位 (現股 + 融資) ===
        if item['type'] == 'Long':
            sells = sym_trans[(sym_trans['trans_type'] == 'Sell') & (sym_trans['mode'] != 'Short')]
            total_sold_qty = sells['qty'].sum()
            
            buys = sym_trans[(sym_trans['trans_type'] == 'Buy') & (sym_trans['mode'] != 'Short')]
            
            temp_sold_allowance = total_sold_qty
            needed_qty = qty_held
            
            for _, row in buys.iterrows():
                buy_qty = row['qty']
                
                # A. 計算自備款與負債 (財務視角)
                # 修改點：使用 'in' 來判斷，避免因資料庫存入 "Margin (融資)" 或有空白而判斷失敗
                mode_str = str(row['mode']) 
                
                if 'Margin' in mode_str: 
                    # --- 融資模式 ---
                    # 成本 = 自備款 (資料庫 cash_flow 絕對值)
                    lot_own_funds = abs(row['cash_flow'])
                    lot_loan = row['loan_amount']
                else: 
                    # --- 現股模式 ---
                    # 成本 = 全額股價 + 手續費
                    lot_own_funds = (row['price'] * row['qty']) + row['fee']
                    lot_loan = 0
                
                # B. 計算全額成本 (交易視角)
                # 這行維持不變，用來顯示平均成本 $70.24
                lot_full_cost = (row['price'] * row['qty']) + row['fee']
                
                if temp_sold_allowance >= buy_qty:
                    temp_sold_allowance -= buy_qty
                    continue
                else:
                    remaining_in_lot = buy_qty - temp_sold_allowance
                    temp_sold_allowance = 0 
                    
                    take_qty = min(remaining_in_lot, needed_qty)
                    ratio = take_qty / buy_qty
                    
                    acc_own_funds += (lot_own_funds * ratio)
                    acc_loan_repay += (lot_loan * ratio)
                    acc_full_basis += (lot_full_cost * ratio)
                    
                    needed_qty -= take_qty
                    if needed_qty <= 0:
                        break
            
            # --- 結算 ---
            est_own_funds = acc_own_funds
            net_equity = mkt_val - acc_loan_repay # 淨值 = 市值 - 負債
            unrealized_pl = net_equity - est_own_funds # 損益 = 淨值 - 本金
            
            # 算出顯示用的平均單價 (全額)
            display_avg_price = acc_full_basis / qty_held if qty_held > 0 else 0
            
        # === 邏輯 B: 做空部位 (融券) ===
        else: # Short
            short_sells = sym_trans[(sym_trans['trans_type'] == 'Sell') & (sym_trans['mode'] == 'Short')]
            covers = sym_trans[(sym_trans['trans_type'] == 'Buy') & (sym_trans['mode'] == 'Short')]
            total_covered_qty = covers['qty'].sum()
            
            temp_covered_allowance = total_covered_qty
            needed_qty = qty_held
            
            acc_deposit = 0    
            acc_collateral = 0 
            
            for _, row in short_sells.iterrows():
                sell_qty = row['qty']
                
                # 財務視角
                lot_deposit = abs(row['cash_flow']) # 保證金
                
                # 交易視角 (當初賣出的全額價值)
                # 用這來看平均賣出價格
                lot_sell_val = row['price'] * row['qty'] 
                
                if temp_covered_allowance >= sell_qty:
                    temp_covered_allowance -= sell_qty
                    continue
                else:
                    remaining_in_lot = sell_qty - temp_covered_allowance
                    temp_covered_allowance = 0
                    
                    take_qty = min(remaining_in_lot, needed_qty)
                    ratio = take_qty / sell_qty
                    
                    acc_deposit += (lot_deposit * ratio)
                    acc_collateral += (lot_sell_val * ratio)
                    
                    needed_qty -= take_qty
                    if needed_qty <= 0:
                        break
            
            est_own_funds = acc_deposit
            unrealized_pl = acc_collateral - mkt_val # (賣出價 - 現價)
            display_avg_price = acc_collateral / qty_held if qty_held > 0 else 0

        # ROI 分母使用「自備款」
        ret_pct = (unrealized_pl / est_own_funds) * 100 if est_own_funds > 0 else 0
        
        total_mkt_val += mkt_val
        total_own_funds += est_own_funds
        
        new_item = item.copy()
        new_item.update({
            'est_own_funds': est_own_funds, # 藏在資料裡供計算用
            'avg_price_basis': display_avg_price, # 這是給 UI 顯示用的「原始均價」
            'unrealized_pl': unrealized_pl,
            'return_pct': ret_pct
        })
        enriched_holdings.append(new_item)
        
    return enriched_holdings, total_mkt_val, total_own_funds
    
@st.cache_data(ttl=3600) # Keep cache for UI speed, but underlying data is persistent
def get_daily_equity_curve(df_trans):
    if df_trans.empty: return pd.DataFrame()
    
    symbols = df_trans['symbol'].unique().tolist()
    
    # === STEP 1: Sync missing data (Incremental Update) ===
    sync_price_history(symbols)
    
    # === STEP 2: Read from Local DB ===
    conn = sqlite3.connect(DB_FILE)
    placeholders = ','.join(['?'] * len(symbols))
    query = f"SELECT date, symbol, close FROM price_history WHERE symbol IN ({placeholders}) ORDER BY date ASC"
    price_df = pd.read_sql_query(query, conn, params=symbols)
    conn.close()
    
    # Check if we have data
    if price_df.empty: return pd.DataFrame()
    
    # Pivot to match the format expected by the calculator (index=Date, columns=Symbols)
    hist_data = price_df.pivot(index='date', columns='symbol', values='close')
    hist_data.index = pd.to_datetime(hist_data.index)
    
    # === STEP 3: Calculate Equity (Same logic as before) ===
    min_date = pd.to_datetime(df_trans['date'].min())
    all_dates = pd.date_range(start=min_date, end=date.today(), freq='D')
    daily_records = []
    
    trans_by_date = df_trans.groupby('date')
    portfolio = {} 
    
    for d in all_dates:
        d_str = d.strftime('%Y-%m-%d')
        if d_str in trans_by_date.groups:
            day_trans = trans_by_date.get_group(d_str)
            for _, t in day_trans.iterrows():
                sym = t['symbol']
                if sym not in portfolio: 
                    portfolio[sym] = {'qty': 0, 'loan': 0, 'short_qty': 0, 'short_deposit': 0, 'short_collateral': 0}
                
                if t['trans_type'] == 'Buy':
                    if t['mode'] == 'Short':
                        portfolio[sym]['short_qty'] -= t['qty']
                        if portfolio[sym]['short_qty'] <= 0:
                             portfolio[sym]['short_qty'] = 0
                             portfolio[sym]['short_deposit'] = 0
                             portfolio[sym]['short_collateral'] = 0
                    else:
                        portfolio[sym]['qty'] += t['qty']
                        portfolio[sym]['loan'] += t['loan_amount']
                elif t['trans_type'] == 'Sell':
                    if t['mode'] == 'Short':
                        portfolio[sym]['short_qty'] += t['qty']
                        portfolio[sym]['short_deposit'] += t['loan_amount']
                        portfolio[sym]['short_collateral'] += (t['price'] * t['qty'])
                    else:
                        portfolio[sym]['qty'] -= t['qty']
                        if t['loan_amount'] > 0: portfolio[sym]['loan'] -= t['loan_amount']

        # Calculate Equity using Local Prices
        total_equity = 0
        try:
            # We use the prices available up to date d
            current_prices = hist_data.loc[:d].iloc[-1]
        except:
            current_prices = pd.Series(0, index=symbols)

        for sym, data in portfolio.items():
            price = current_prices.get(sym, 0)
            if pd.isna(price): price = 0
            
            if data['qty'] > 0:
                total_equity += (price * data['qty']) - data['loan']
            
            if data['short_qty'] > 0:
                unrealized = data['short_collateral'] - (price * data['short_qty'])
                total_equity += data['short_deposit'] + unrealized
        
        daily_records.append({'date': d, 'equity': total_equity})

    return pd.DataFrame(daily_records)

@st.cache_data
def calculate_performance_kpis(df):
    """
    計算績效指標：總損益、勝率、盈虧比、平均盈虧
    """
    if df.empty:
        return None

    total_profit = df['realized_pl'].sum()
    trade_count = len(df)
    
    # 勝率計算
    wins = df[df['realized_pl'] > 0]
    losses = df[df['realized_pl'] <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0
    
    # 盈虧比 (PF)
    gross_profit = wins['realized_pl'].sum()
    gross_loss = abs(losses['realized_pl'].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
    
    # 平均盈虧
    avg_win = wins['realized_pl'].mean() if not wins.empty else 0
    avg_loss = losses['realized_pl'].mean() if not losses.empty else 0
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    return {
        'total_profit': total_profit,
        'win_rate': win_rate,
        'win_count': win_count,
        'loss_count': loss_count,
        'profit_factor': profit_factor,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'rr_ratio': rr_ratio
    }

@st.cache_data
def get_pnl_chart_data(df):
    """
    準備圖表資料：累計損益曲線、個股排行
    """
    if df.empty: return pd.DataFrame(), pd.Series()

    # 1. 累計損益曲線 (需按日期排序並累加)
    # 同一天先加總，避免線條回頭
    daily_group = df.groupby('date')['realized_pl'].sum().reset_index().sort_values('date')
    daily_group['cumulative_pl'] = daily_group['realized_pl'].cumsum()
    
    # 2. 個股排行
    stock_rank = df.groupby('stock_name')['realized_pl'].sum().sort_values(ascending=True)
    
    return daily_group, stock_rank
    
# --- 介面 Callback ---
def validate_stock_callback():
    code = st.session_state.input_code_raw
    st.session_state.valid_symbol = None
    st.session_state.valid_name = "" 
    
    if code:
        # --- 1. 優先查詢資料庫 (DB First) ---
        # 如果資料庫裡已經有這支股票，就直接用上次設定的名稱
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        candidates = [code]
        if not code.endswith('.TW') and not code.endswith('.TWO'):
            candidates.append(f"{code}.TW")
            candidates.append(f"{code}.TWO")
        
        found_in_db = False
        for cand in candidates:
            c.execute("SELECT stock_name, symbol FROM transactions WHERE symbol=? ORDER BY date DESC, id DESC LIMIT 1", (cand,))
            row = c.fetchone()
            if row:
              
                st.session_state.valid_name = row[0]   # 自動代入習慣名稱 (例如: 台積電-長投)
                st.session_state.valid_symbol = row[1] # 使用正確的後綴代號 (例如: 2330.TW)
                found_in_db = True
                break
        
        conn.close()
        
        if found_in_db:
            return # 資料庫有紀錄，直接結束，不用查 Yahoo

        # --- 2. 資料庫沒有，才去問 Yahoo Finance (Fallback) ---
        suffixes = ['.TW', '.TWO']
        for s in suffixes:
            test = f"{code}{s}" if not code.endswith(s) else code
            try:
                t = yf.Ticker(test)
                if t.fast_info.last_price:
                    st.session_state.valid_symbol = test
                    st.session_state.valid_name = test # 若是新股票，預設名稱就用代號
                    return
            except: pass
        
        st.session_state.valid_error = "查無此股"

# --- 主程式 ---
def main():
    init_db()
    
    if 'valid_symbol' not in st.session_state: st.session_state.valid_symbol = None
    if 'valid_name' not in st.session_state: st.session_state.valid_name = ""

    st.title("資產管理系統 ")

    # === 左側輸入區 ===
    with st.sidebar:
        st.header("📝 交易登錄")
        st.text_input("股票代碼", key="input_code_raw", on_change=validate_stock_callback, placeholder="輸入 2330 按 Enter")
        
        if st.session_state.valid_symbol:
            st.success(f"鎖定: {st.session_state.valid_symbol}")
            
            input_name = st.text_input("股票名稱", value=st.session_state.valid_name)
            st.session_state.valid_name = input_name
            input_date = st.date_input("交易日期", date.today())
            
            mode_opts = ["Spot (現股)", "Margin (融資)", "Short (融券)", "Day Trade (現股當沖)"]
            mode_raw = st.selectbox("模式", mode_opts)
            
            mode = "Spot"
            if "Margin" in mode_raw: mode = "Margin"
            if "Short" in mode_raw: mode = "Short"
            if "Day Trade" in mode_raw: mode = "Day Trade"

            qty = st.number_input("股數", min_value=10, step=1000)

            price = 0.0      
            t_type = "Buy"   
            
            if mode == "Day Trade":
                st.info("⚡ 當沖模式：稅率 0.15%")
                c_dt1, c_dt2 = st.columns(2)
                buy_price = c_dt1.number_input("買入價格", min_value=0.0, step=0.1)
                sell_price = c_dt2.number_input("賣出價格", min_value=0.0, step=0.1)
                
                if buy_price > 0 and sell_price > 0:
                    f_buy = max(20, int(buy_price * qty * 0.001425 * 0.6))
                    f_sell = max(20, int(sell_price * qty * 0.001425 * 0.6))
                    t_dt = int(sell_price * qty * 0.0015)
                    dt_profit = (sell_price * qty - f_sell - t_dt) - (buy_price * qty + f_buy)
                    
                    color = "normal" if dt_profit > 0 else "inverse"
                    st.metric("預估當沖獲利", f"${dt_profit:,}", delta_color=color)

            else:
                c1, c2 = st.columns(2)
                t_type = c1.selectbox("方向", ["Buy", "Sell"])
                c2.write("") 
                
                price = st.number_input("價格", min_value=0.0, step=0.1)
                
                loan_val = 0
                ratio_val = 0.0
                interest_est = 0
                profit_est = 0
                cost_basis_est = 0
                
                if mode == "Margin" and t_type == "Buy":
                    st.info("設定融資成數")
                    ratio_val = st.slider("成數", 0.1, 0.9, 0.6, 0.1)
                    loan_val = int(price * qty * ratio_val)
                    st.caption(f"自備: ${int(price*qty - loan_val):,} | 借款: ${loan_val:,}")

                elif mode == "Margin" and t_type == "Sell":
                    st.warning("系統將依 FIFO 自動計算償還本金與利息")
                    if st.button("🔍 試算損益"):
                         loan_val, interest_est, cost_basis_est = calculate_fifo_outcome(st.session_state.valid_symbol, qty, price, input_date, mode)
                         
                         fee_tmp = max(20, int(price*qty*0.001425*0.6))
                         tax_tmp = int(price*qty*0.003)
                         profit_est = (price * qty) - fee_tmp - tax_tmp - loan_val - interest_est - cost_basis_est
                         
                         st.write(f"償還本金: ${loan_val:,}")
                         st.write(f"利息: ${interest_est:,}")
                         st.metric("預估獲利", f"${profit_est:,.0f}", delta_color="normal")

                elif mode == "Short" and t_type == "Sell":
                    ratio_val = st.slider("保證金成數", 0.1, 1.0, 0.9, 0.1)
                    loan_val = int(price * qty * ratio_val)
                    st.caption(f"需繳保證金: ${loan_val:,}")

                raw_fee = math.floor(price * qty * 0.001425 * 0.6)
                fee = max(20, raw_fee)
                tax = 0
                if t_type == 'Sell':
                    rate = 0.001 if st.session_state.valid_symbol.startswith('00') else 0.003
                    tax = math.floor(price * qty * rate)
                
                st.markdown("---")
                c3, c4 = st.columns(2)
                c3.write(f"手續費: {fee}")
                c4.write(f"稅: {tax}")

            if st.button("確認交易", type="primary", width='stretch'):
                
                if mode == "Day Trade":
                    if buy_price <= 0 or sell_price <= 0:
                        st.error("請輸入完整的買入與賣出價格")
                    else:
                        fee_buy = max(20, int(buy_price * qty * 0.001425 * 0.6))
                        add_transaction(input_date, st.session_state.valid_symbol, st.session_state.valid_name,
                                        'Buy', 'Day Trade', buy_price, qty, fee_buy, 0, 
                                        0, 0, 0, 0)
                        
                        fee_sell = max(20, int(sell_price * qty * 0.001425 * 0.6))
                        tax_dt = int(sell_price * qty * 0.0015)
                        
                        buy_cost_total = (buy_price * qty) + fee_buy
                        sell_net_total = (sell_price * qty) - fee_sell - tax_dt
                        realized_pl = sell_net_total - buy_cost_total
                        
                        add_transaction(input_date, st.session_state.valid_symbol, st.session_state.valid_name,
                                        'Sell', 'Day Trade', sell_price, qty, fee_sell, tax_dt, 
                                        0, 0, realized_pl, buy_cost_total)
                        
                        st.success("已完成當沖登錄！")
                        time.sleep(1) 
                        st.rerun()

                else:
                    realized_pl = 0
                    cost_basis = 0
                    final_loan_param = loan_val 
                    final_interest_param = 0
                    
                    if mode == 'Margin' and t_type == 'Sell':
                        repay_loan, interest_calc, orig_invested = calculate_fifo_outcome(st.session_state.valid_symbol, qty, price, input_date, mode)
                        
                        final_loan_param = repay_loan
                        final_interest_param = interest_calc
                        current_net_cash = (price * qty) - fee - tax - repay_loan - interest_calc
                        realized_pl = current_net_cash - orig_invested

                    elif mode == 'Short' and t_type == 'Buy':
                        refund_deposit, refund_collateral, orig_invested = calculate_fifo_outcome(st.session_state.valid_symbol, qty, price, input_date, mode)
                        total_refund = refund_deposit + refund_collateral
                        cover_cost = (price * qty) + fee
                        current_net_cash = total_refund - cover_cost
                        final_loan_param = total_refund 
                        realized_pl = current_net_cash - orig_invested
                        
                    elif mode == 'Spot' and t_type == 'Sell':
                         _, _, orig_invested = calculate_fifo_outcome(st.session_state.valid_symbol, qty, price, input_date, mode)
                         current_net_cash = (price * qty) - fee - tax
                         realized_pl = current_net_cash - orig_invested

                    add_transaction(input_date, st.session_state.valid_symbol, st.session_state.valid_name,
                                    t_type, mode, price, qty, fee, tax, 
                                    final_loan_param, final_interest_param, realized_pl, cost_basis)
                    st.success("已記錄！")
                    time.sleep(1) 
                    st.rerun()
            

    # === 右側主畫面 ===
    df = get_transactions(st.session_state.db_updated)
    
    tab1, tab2, tab3= st.tabs(["📊 資產儀表板", "💰 已實現損益", "📝 交易明細"])

    # 計算數據 
    active_syms, holdings_map, symbol_name_map = calculate_current_holdings(df)
    
    current_holdings = [] 
    total_equity_val = 0
    total_debt_val = 0
    total_mkt_val = 0
    
    st.markdown("---")
    with st.expander("🛠️ 系統維護 (進階)"):
        st.caption("如果你曾補登以前日期的交易，請執行此功能以校正損益。")
        
        if st.button("🔄 重算所有歷史損益 (Replay)"):
            with st.spinner("正在搭乘時光機重跑交易紀錄..."):
                try:
                    recalculate_history()
                    st.success("✅ 歷史損益重算完成！FIFO 順序已校正。")
                    time.sleep(1) 
                    st.rerun()
                except Exception as e:
                    st.error(f"重算失敗: {e}")
        if st.button("🔄 強制重抓所有股價 (修正除權息)"):
            conn = sqlite3.connect(DB_FILE)
            conn.execute("DELETE FROM price_history") 
            conn.commit()
            conn.close()
            st.cache_data.clear()
            st.success("已清除歷史快取，請重新整理頁面以重抓資料。")
        
    if active_syms:
        current_prices = fetch_current_prices(active_syms)
        
        for sym in active_syms:
            curr_p = current_prices.get(sym, 0)
            h = holdings_map[sym]
            s_name = symbol_name_map.get(sym, sym) 
            
            # Long Equity
            long_equity = 0
            if h['qty'] > 0:
                mkt = curr_p * h['qty']
                long_equity = mkt - h['loan']
                total_mkt_val += mkt
                total_debt_val += h['loan']
                
                current_holdings.append({
                    'symbol': sym, 
                    'stock_name': s_name, 
                    'type': 'Long', 
                    'val': mkt,                 
                    'qty': int(h['qty']),       
                    'display_mkt': mkt          
                })    

            # Short Equity
            short_equity = 0
            if h['short_qty'] > 0:
                mkt_cost_to_cover = curr_p * h['short_qty']
                unrealized = h['short_collateral'] - mkt_cost_to_cover
                short_equity = h['short_deposit'] + unrealized
                
                current_holdings.append({
                    'symbol': sym, 
                    'stock_name': s_name, 
                    'type': 'Short', 
                    'val': h['short_deposit'], 
                    'qty': int(h['short_qty']),        
                    'display_mkt': mkt_cost_to_cover   
                })
            total_equity_val += (long_equity + short_equity)
            
    # --- Tab 1: 儀表板 & 資產走勢 ---
    with tab1:

        if current_holdings:

            # === 1. 呼叫快取函數 (極速) ===
            enriched_holdings, total_mkt_val, total_own_funds = calculate_portfolio_metrics(df, current_holdings)

            total_pl = sum(x['unrealized_pl'] for x in enriched_holdings)

            total_return_pct = (total_pl / total_own_funds) * 100 if total_own_funds > 0 else 0

            

            with st.container():

                m1, m2, m3 = st.columns(3)

                m1.metric("總持倉市值", f"${total_own_funds:,.0f}")

                pl_color = "normal" if total_pl >= 0 else "inverse"

                m2.metric("總未實現損益", f"${total_pl:,.0f}", delta_color=pl_color)

                m3.metric("總報酬率", f"{total_return_pct:.2f}%", delta_color=pl_color)



            st.divider()



            # === 第二列：圓餅圖 (左) + 長條圖 (右) ===

            c_chart1, c_chart2 = st.columns(2)



            with c_chart1:

                # 1. 圓餅圖 (配置) 

                labels = [x['stock_name'] for x in enriched_holdings]

                values = [x['val'] for x in enriched_holdings] # 使用權重基準

                

                fig_pie = go.Figure(data=[go.Pie(

                    labels=labels,

                    values=values,

                    textinfo='percent+label',

                    hole=0.4, # 甜甜圈圖比較好看

                    insidetextorientation='radial'

                )])

                fig_pie.update_layout(title_text="資產配置 (權重)", margin=dict(t=30, b=0, l=0, r=0))

                st.plotly_chart(fig_pie, width='stretch')



            with c_chart2:

                # 2. 長條圖 (個股損益) 

                # 準備顏色：紅賺綠賠

                colors = ['#FF5252' if x['unrealized_pl'] >= 0 else '#00C853' for x in enriched_holdings]

                

                fig_bar = go.Figure(data=[go.Bar(

                    x=[x['stock_name'] for x in enriched_holdings],

                    y=[x['unrealized_pl'] for x in enriched_holdings],

                    marker_color=colors,

                    text=[f"${x['unrealized_pl']/1000:.1f}k" for x in enriched_holdings], # 顯示 k 為單位

                    textposition='auto',

                )])

                fig_bar.update_layout(

                    title_text="個股未實現損益",

                    xaxis_title="",

                    yaxis_title="損益 (TWD)",

                    margin=dict(t=30, b=0, l=0, r=0)

                )

                st.plotly_chart(fig_bar, width='stretch')



            st.divider()



            # === 第三列：詳細表格 (紅綠文字著色) ===
            st.subheader("📋 持倉明細")
            
            df_show = pd.DataFrame(enriched_holdings)
            
            # --- 修改部分開始 ---
            # 1. 為了顯示，我們直接使用剛剛算好的 avg_price_basis
            df_show['display_cost_per_share'] = df_show['avg_price_basis']
            
            # 2. 現價的計算維持不變
            df_show['curr_price'] = df_show.apply(lambda x: x['display_mkt'] / x['qty'] if x['qty'] != 0 else 0, axis=1)
            
            # 3. 選取欄位：將 'display_cost_per_share' 對應到 '平均成本'
            cols_map = {
                'stock_name': '名稱',
                'symbol': '代碼',
                'qty': '庫存股數',
                'display_cost_per_share': '平均成本',  # <--- 這裡改用新的欄位
                'curr_price': '現價',
                'unrealized_pl': '損益金額',
                'return_pct': '報酬率 %'
            }

            df_table = df_show[list(cols_map.keys())].rename(columns=cols_map)



            # 3. 定義樣式函數

            

            # (A) 給損益欄位用的 (單純看數值正負)

            def color_profit(val):

                color = '#FF5252' if val > 0 else '#00C853' if val < 0 else 'white'

                return f'color: {color}; font-weight: bold;'



            # (B) [新增] 給「現價」欄位用的 (依據損益來決定顏色)

            # 這是 Row-wise 的樣式邏輯

            def color_price_col(row):

                # 判斷依據：看該列的「損益金額」是賺還是賠

                val = row['損益金額']

                color = '#FF5252' if val > 0 else '#00C853' if val < 0 else 'white'

                

                # 回傳樣式列表，只針對「現價」欄位上色，其他欄位空白

                return [f'color: {color}; font-weight: bold;' if col == '現價' else '' for col in row.index]



            st.dataframe(

                df_table.style

                .format({

                    '平均成本': '${:,.2f}',  # 成本建議顯示小數點

                    '現價': '${:,.2f}',      # 現價建議顯示小數點

                    '損益金額': '${:+,.0f}',

                    '報酬率 %': '{:+.2f}%',

                    '庫存股數': '{:,}'

                })

                # 1. 針對 損益與報酬率 上色 (看該格數值)

                .map(color_profit, subset=['損益金額', '報酬率 %'])

                # 2. [新增] 針對 現價 上色 (看整列損益狀況)

                .apply(color_price_col, axis=1)

                # 3. 背景長條圖

                .bar(subset=['損益金額'], align='mid', color=['#00C853', '#FF5252']),

                width='stretch',

                hide_index=True

            )



        else:

            st.info("目前無持股，請新增交易。")

    with tab2:
        st.subheader("🏆 獲利績效分析")
        
        # 0. 基礎資料 (只取已平倉)
        # 這裡建議在 get_transactions 就先做完 to_datetime 轉換，或是這裡做一次
        closed_trades = df[df['realized_pl'] != 0].copy()
        
        if closed_trades.empty:
            st.info("尚未有賣出獲利紀錄。")
        else:
            # 確保有 date_obj 欄位供篩選
            if 'date_obj' not in closed_trades.columns:
                closed_trades['date_obj'] = pd.to_datetime(closed_trades['date']).dt.date

            # === 1. 日期篩選區 ===
            with st.container():
                min_d, max_d = closed_trades['date_obj'].min(), closed_trades['date_obj'].max()
                c_filter1, _ = st.columns([2, 1])
                date_range = c_filter1.date_input("📅 篩選區間", value=(min_d, max_d), min_value=min_d, max_value=max_d)
                
                # 執行篩選
                target_df = closed_trades
                if isinstance(date_range, tuple) and len(date_range) == 2:
                    start_d, end_d = date_range
                    target_df = closed_trades[(closed_trades['date_obj'] >= start_d) & (closed_trades['date_obj'] <= end_d)]

            st.divider()

            if target_df.empty:
                st.warning("此區間無交易紀錄。")
            else:
                # === 2. 呼叫快取函數 (計算 KPI) ===
                kpi = calculate_performance_kpis(target_df)
                
                # 顯示 KPI
                k1, k2, k3, k4 = st.columns(4)
                
                color_pl = "normal" if kpi['total_profit'] >= 0 else "inverse"
                k1.metric("區間總損益", f"${kpi['total_profit']:,.0f}", delta_color=color_pl)
                k2.metric("交易勝率", f"{kpi['win_rate']:.1f}%", f"{kpi['win_count']}勝 {kpi['loss_count']}敗")
                
                pf_str = f"{kpi['profit_factor']:.2f}" if kpi['profit_factor'] != float('inf') else "∞"
                k3.metric("盈虧比 (PF)", pf_str)
                k4.metric("平均 賺:賠", f"${kpi['avg_win']:,.0f} : ${abs(kpi['avg_loss']):,.0f}", f"比率 {kpi['rr_ratio']:.1f}")

                # === 3. 呼叫快取函數 (準備圖表資料) ===
                daily_pl_df, stock_rank_series = get_pnl_chart_data(target_df)

                # A. 畫資金曲線
                fig_cum = px.line(daily_pl_df, x='date', y='cumulative_pl', title="📈 資金累計損益曲線", markers=True)
                fig_cum.update_traces(line_color='#2962FF', fill='tozeroy')
                fig_cum.update_layout(xaxis_title="", yaxis_title="累計損益", hovermode="x unified")
                st.plotly_chart(fig_cum, width='stretch')

                # B. 畫個股排行
                st.subheader("📊 個股損益貢獻")
                colors = ['#FF5252' if v >= 0 else '#00C853' for v in stock_rank_series.values]
                
                fig_bar = go.Figure(data=[go.Bar(
                    y=stock_rank_series.index, 
                    x=stock_rank_series.values, 
                    orientation='h', 
                    marker_color=colors,
                    text=[f"${v:,.0f}" for v in stock_rank_series.values],
                    textposition='auto'
                )])
                fig_bar.update_layout(title="個股損益排行", height=400 + (len(stock_rank_series)*15))
                st.plotly_chart(fig_bar, width='stretch')

                # === 4. 明細表格 (UI 直接顯示即可，不需快取) ===
                st.markdown("---")
                st.subheader("📝 交易明細表")
                
                display_cols = ['date', 'symbol', 'stock_name', 'trans_type', 'mode', 'qty', 'price', 'realized_pl']
                df_table = target_df[display_cols].sort_values('date', ascending=False)
                df_table.columns = ['日期', '代碼', '名稱', '方向', '模式', '股數', '價格', '損益']
                
                def color_pl_column(val):
                    color = '#FF5252' if val > 0 else '#00C853' if val < 0 else 'white'
                    return f'color: {color}; font-weight: bold;'

                st.dataframe(
                    df_table.style.format({'價格': '{:.2f}', '損益': '{:+,.0f}', '股數': '{:,}'})
                    .map(color_pl_column, subset=['損益'])
                    .bar(subset=['損益'], align='mid', color=['#00C853', '#FF5252']),
                    width='stretch', hide_index=True
                )

    # --- Tab 3: 交易流水帳 ---
    with tab3:
        st.dataframe(df.sort_values(['date', 'id'], ascending=False), width="stretch")
        
        with st.expander("🗑️ 刪除交易"):
            del_id = st.number_input("輸入 ID", step=1)
            if st.button("刪除"):
                conn = sqlite3.connect(DB_FILE)
                conn.execute("DELETE FROM transactions WHERE id=?", (del_id,))
                conn.commit()
                conn.close()
                trigger_db_update()
                st.success(f"ID {del_id} 已刪除")
                time.sleep(1) 
                st.rerun()

        with st.expander("✏️ 編輯股票名稱"):
            edit_mode = st.radio(
                "編輯模式", 
                ["批量修改 (指定代碼)", "單筆修改 (指定 ID)"], 
                horizontal=True,
                key='edit_mode_radio'
            )
            
            # === Mode 1: 批量修改 (Default) ===
            if edit_mode == "批量修改 (指定代碼)": 
                unique_symbols = df['symbol'].unique().tolist() if not df.empty else []
                with st.form(key="batch_edit_form"):
                    c_batch1, c_batch2 = st.columns([1, 2])
                    target_symbol = c_batch1.selectbox("選擇要修改的代碼", unique_symbols, key="edit_target_sym")
                    new_name_batch = c_batch2.text_input("輸入統一新名稱", key="new_name_batch")

                    submitted_batch = st.form_submit_button(f"確認批量更新")
                    
                    if submitted_batch:
                        if not new_name_batch.strip():
                            st.error("請輸入新名稱")
                        else:
                            conn = sqlite3.connect(DB_FILE)
                            conn.execute("UPDATE transactions SET stock_name=? WHERE symbol=?", (new_name_batch, target_symbol))
                            conn.commit()
                            conn.close()
                            trigger_db_update()
                            st.success(f"代碼 {target_symbol} 的所有交易名稱已統一更新為：{new_name_batch}")
                            time.sleep(1) 
                            st.rerun()

            # === Mode 2: 單筆修改 ===
            else:
                with st.form(key="single_edit_form"):
                    c_edit1, c_edit2 = st.columns([1, 2])
                    target_id = c_edit1.number_input("輸入要修改的 ID", min_value=1, step=1, key="edit_target_id")
                    new_name = c_edit2.text_input("輸入新名稱", key="new_name_id")
                    
                    submitted_single = st.form_submit_button("確認單筆更新")
                    
                    if submitted_single:
                        if not new_name.strip():
                            st.error("請輸入新名稱")
                        else:
                            conn = sqlite3.connect(DB_FILE)
                            conn.execute("UPDATE transactions SET stock_name=? WHERE id=?", (new_name, target_id))
                            conn.commit()
                            conn.close()
                            trigger_db_update()
                            st.success(f"ID {target_id} 名稱已更新為：{new_name}")
                            time.sleep(1) 
                            st.rerun()

if __name__ == "__main__":
    main()