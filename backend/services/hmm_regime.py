"""
Hidden Markov Model (HMM) Regime Classifier.

Replaces deterministic rules with a probabilistic inference model that learns 
hidden market states from the covariance of SPY returns and Volatility.
"""
import logging
import pandas as pd
import numpy as np
from typing import Optional, Dict

logger = logging.getLogger(__name__)

_hmm_model = None
_last_fit_ts = 0.0

def fit_and_predict() -> Optional[Dict[str, float]]:
    global _hmm_model, _last_fit_ts
    import time
    now = time.time()
    
    try:
        from hmmlearn.hmm import GaussianHMM
        from services.data_fetcher import fetch_ohlcv
    except ImportError:
        logger.debug("hmmlearn not installed. Run `pip install hmmlearn` to enable HMM.")
        return None
        
    try:
        spy = fetch_ohlcv("SPY", "1d")
        vix = fetch_ohlcv("^VIX", "1d")
        if spy is None or vix is None or len(spy) < 252:
            return None
        df = pd.DataFrame()
        df["spy_ret"] = np.log(spy["Close"] / spy["Close"].shift(1))
        df["spy_vol"] = df["spy_ret"].rolling(20).std()
        df["vix"] = vix["Close"]
        df = df.dropna()
        if len(df) < 200:
            return None
        X = df[["spy_ret", "spy_vol", "vix"]].values

        if _hmm_model is None or (now - _last_fit_ts > 86400):  # Retrain daily
            model = GaussianHMM(n_components=3, covariance_type="full", n_iter=100, random_state=42)
            model.fit(X)
            _hmm_model = model
            _last_fit_ts = now
            
        # Evaluate current state: sort hidden states by their average VIX level
        # Features: 0=spy_ret, 1=spy_vol, 2=vix
        vix_means = _hmm_model.means_[:, 2]
        sorted_states = np.argsort(vix_means)
        state_map = {
            sorted_states[0]: "bull",  # Lowest VIX mean
            sorted_states[1]: "chop",  # Mid VIX mean
            sorted_states[2]: "bear",  # Highest VIX mean
        }
        
        curr_hidden_state = _hmm_model.predict(X[-1:])[0]
        return {
            "current_state": state_map[curr_hidden_state]
        }
    except Exception as e:
        logger.debug(f"HMM fit failed: {e}")
        return None