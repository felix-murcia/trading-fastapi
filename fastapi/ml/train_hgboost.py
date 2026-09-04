import urllib.request
import json
import pandas as pd
import numpy as np
import pickle
import os
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.metrics import accuracy_score, classification_report

MT5_HTTP_URL = os.getenv("MT5_HTTP_URL")  # REQUIRED - no default
SYMBOL = "EURUSD"
TIMEFRAME = "M15"
COUNT = 50000 

def fetch_data():
    print(f"Fetching {COUNT} candles for {SYMBOL} ({TIMEFRAME})...")
    url = f"{MT5_HTTP_URL}/api/v1/market/candles/latest?symbol_name={SYMBOL}&timeframe={TIMEFRAME}&count={COUNT}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode('utf-8'))
    candles = data if isinstance(data, list) else data.get("candles", [])
    df = pd.DataFrame(candles)
    return df

def feature_engineering(df):
    print("Generating ADVANCED statistical features (MACD, Bollinger, Momentum)...")
    df['returns'] = df['close'].pct_change()
    df['range'] = (df['high'] - df['low']) / df['open']
    
    df['sma20'] = df['close'].rolling(20).mean()
    df['dist_sma20'] = (df['close'] - df['sma20']) / df['sma20']
    
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ema_up = up.rolling(14).mean()
    ema_down = down.rolling(14).mean()
    rs = ema_up / ema_down
    df['rsi14'] = 100 - (100 / (1 + rs))
    
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    std20 = df['close'].rolling(20).std()
    df['bb_upper'] = df['sma20'] + (std20 * 2.5)
    df['bb_lower'] = df['sma20'] - (std20 * 2.5)
    bb_range = df['bb_upper'] - df['bb_lower']
    bb_range = bb_range.replace(0, np.nan)
    df['bb_pos'] = (df['close'] - df['bb_lower']) / bb_range
    
    for i in [1, 2, 3, 5]:
        df[f'lag_return_{i}'] = df['close'].pct_change(i)
        
    df['hour'] = pd.to_datetime(df['time'], unit='s').dt.hour
    
    df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    return df.dropna()

def train():
    df = fetch_data()
    if df.empty:
        print("Dataframe is empty. Aborting.")
        return
    df = feature_engineering(df)
    
    features = [
        'returns', 'range', 'dist_sma20', 'rsi14', 
        'macd', 'macd_signal', 'macd_hist', 'bb_pos',
        'lag_return_1', 'lag_return_2', 'lag_return_3', 'lag_return_5',
        'hour'
    ]
    
    X = df[features]
    y = df['target']
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.15, shuffle=False)
    
    print("Executing GridSearch over HistGradientBoostingClassifier (LightGBM equivalent)...")
    param_grid = {
        'learning_rate': [0.01, 0.05],
        'max_iter': [100, 200],
        'max_depth': [4, 6],
        'l2_regularization': [0.0, 0.1]
    }
    
    hgb = HistGradientBoostingClassifier(random_state=42)
    grid = GridSearchCV(estimator=hgb, param_grid=param_grid, cv=3, scoring='accuracy', n_jobs=-1, verbose=1)
    grid.fit(X_train, y_train)
    
    print(f"Mejores Hiperparametros: {grid.best_params_}")
    
    best_model = grid.best_estimator_
    preds = best_model.predict(X_test)
    acc = accuracy_score(y_test, preds)
    
    print(f"Accuracy Final (Out-Of-Sample): {acc*100:.2f}%")
    print(classification_report(y_test, preds))
    
    with open('/app/ml/model.pkl', 'wb') as f:
        pickle.dump({'model': best_model, 'features': features}, f)
        
    print("Modelo HGBC Elite guardado en /app/ml/model.pkl.")

if __name__ == "__main__":
    train()
