import urllib.request
import json
import pandas as pd
import numpy as np
import pickle
import os
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report

MT5_HTTP_URL = os.getenv("MT5_HTTP_URL")  # REQUIRED - no default
SYMBOL = "EURUSD"
TIMEFRAME = "M15"
COUNT = 25000  # ~1 year of M15 candles

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
    print("Generating statistical features...")
    # Basic technicals
    df['returns'] = df['close'].pct_change()
    df['range'] = (df['high'] - df['low']) / df['open']
    
    # Moving Average Distance
    df['sma20'] = df['close'].rolling(20).mean()
    df['dist_sma20'] = (df['close'] - df['sma20']) / df['sma20']
    
    # RSI 14
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ema_up = up.rolling(14).mean()
    ema_down = down.rolling(14).mean()
    rs = ema_up / ema_down
    df['rsi14'] = 100 - (100 / (1 + rs))
    
    # Target: 1 if next close > current close, 0 otherwise
    df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    
    # Drop NaNs created by rolling and end lag
    df = df.dropna()
    return df

def train():
    df = fetch_data()
    if df.empty:
        print("Dataframe is empty. Aborting.")
        return
    df = feature_engineering(df)
    
    features = ['returns', 'range', 'dist_sma20', 'rsi14']
    X = df[features]
    y = df['target']
    
    # Split
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, shuffle=False)
    
    # Model
    print("Training RandomForest Classifier...")
    model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
    model.fit(X_train, y_train)
    
    # Eval
    preds = model.predict(X_test)
    acc = accuracy_score(y_test, preds)
    print(f"Validation Accuracy: {acc*100:.2f}%")
    print(classification_report(y_test, preds))
    
    # Save
    with open('/app/ml/model.pkl', 'wb') as f:
        pickle.dump({'model': model, 'features': features}, f)
    print("Saved to /app/ml/model.pkl")

if __name__ == "__main__":
    os.makedirs('/app/ml', exist_ok=True)
    train()
