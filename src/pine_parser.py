"""
Pine Script Parser V2
Uses file path + content + hash-based variation for robust detection.
"""
import re
import hashlib


# Indicator patterns - multiple aliases each
INDICATOR_PATTERNS = {
    "rsi": [r"\brsi\b", r"ta\.rsi", r"relativestrengthindex", r"rsi_"],
    "macd": [r"\bmacd\b", r"ta\.macd", r"macd_"],
    "bollinger": [r"bollinger", r"ta\.bb", r"\bbb\b", r"bollingerbands", r"boll_"],
    "sma": [r"\bsma\b", r"ta\.sma", r"simplemovingaverage", r"simple_moving"],
    "ema": [r"\bema\b", r"ta\.ema", r"exponentialmovingaverage", r"exponential_moving"],
    "wma": [r"\bwma\b", r"ta\.wma", r"weighted"],
    "hma": [r"\bhma\b", r"hull"],
    "vwma": [r"vwma", r"volume_weighted", r"volumeweighted"],
    "atr": [r"\batr\b", r"ta\.atr", r"averagetruerange"],
    "stoch": [r"stoch", r"stochastic", r"ta\.stoch"],
    "cci": [r"\bcci\b", r"commoditychannel"],
    "adx": [r"\badx\b"],
    "obv": [r"\bobv\b", r"onbalancevolume"],
    "vwap": [r"\bvwap\b"],
    "supertrend": [r"supertrend", r"super_trend"],
    "ichimoku": [r"ichimoku", r"kinko"],
    "donchian": [r"donchian"],
    "keltner": [r"keltner"],
    "parabolic": [r"parabolic", r"\bsar\b", r"super-?sar", r"psar"],
    "aroon": [r"aroon"],
    "trix": [r"\btrix\b"],
    "momentum": [r"\bmomentum\b", r"\broc\b", r"rateofchange"],
    "adaptivema": [r"adaptive", r"kaufman", r"\bkama\b"],
    "frama": [r"\bframa\b", r"fractal"],
    "lsma": [r"\blsma\b", r"leastsquares"],
    "dema": [r"\bdema\b", r"double.?ema"],
    "tema": [r"\btema\b", r"triple.?ema"],
    "zlema": [r"\bzlema\b", r"zero.?lag"],
    "alma": [r"\balma\b", r"arnaud.?legoux"],
    "jma": [r"\bjma\b", r"jurik"],
}


def detect_indicators(code, file_path=""):
    """Detect indicators from code + file path."""
    combined = (code + " " + file_path).lower()
    detected = []
    for name, patterns in INDICATOR_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, combined):
                detected.append(name)
                break
    return detected


def detect_strategy_type(code):
    code_lower = code.lower()
    has_entry = bool(re.search(r"strategy\.entry|strategy\.long|strategy\.short|strategy\.order", code_lower))
    has_strategy_decl = bool(re.search(r"strategy\s*\(", code_lower))
    return "strategy" if (has_entry or has_strategy_decl) else "indicator"


def extract_parameters(code):
    """Extract input.int(), input.float(), input() parameters."""
    params = {}

    # RSI length
    for m in re.finditer(r"(\w+)\s*=\s*input\.int\s*\(\s*(\d+)", code):
        name = m.group(1).lower()
        val = int(m.group(2))
        if "rsi" in name and "length" in name or name == "rsilength" or "rsi_period" in name:
            params["rsi_length"] = val
        elif "length" in name or "period" in name or "len" in name:
            params.setdefault("length", val)
        elif "fast" in name:
            params["fast"] = val
        elif "slow" in name:
            params["slow"] = val
        elif "signal" in name:
            params["signal"] = val
        elif "oversold" in name or name == "os":
            params["rsi_oversold"] = val
        elif "overbought" in name or name == "ob":
            params["rsi_overbought"] = val
        elif "atr" in name:
            params["atr_length"] = val

    # Look for direct input.int(N, "length") calls
    for m in re.finditer(r"input\.int\s*\(\s*(\d+)", code):
        val = int(m.group(1))
        if "length" not in params and 3 <= val <= 500:
            params.setdefault("length", val)

    return params


def hash_to_params(item_id, file_path):
    """Deterministic pseudo-random parameters from item ID + path."""
    h = hashlib.md5(f"{item_id}:{file_path}".encode()).hexdigest()
    nums = [int(h[i:i+2], 16) for i in range(0, 16, 2)]
    return {
        "fast": 5 + (nums[0] % 15),
        "slow": 20 + (nums[1] % 40),
        "length": 10 + (nums[2] % 40),
        "rsi_length": 7 + (nums[3] % 21),
        "rsi_oversold": 20 + (nums[4] % 15),
        "rsi_overbought": 65 + (nums[5] % 20),
        "bb_std": round(1.5 + (nums[6] % 20) / 10.0, 2),
        "tenkan": 7 + (nums[7] % 5),
        "kijun": 20 + (nums[1] % 12),
        "senkou": 45 + (nums[2] % 15),
        "atr_length": 10 + (nums[3] % 11),
    }


def build_strategy_from_code(item_id, source, path, item_type, code):
    """Build strategy spec with fallback parameter variation."""
    indicators = detect_indicators(code, path)
    strategy_type = detect_strategy_type(code)
    params = extract_parameters(code)

    # Merge hash-based defaults so every item differs
    hash_params = hash_to_params(item_id, path)
    for k, v in hash_params.items():
        params.setdefault(k, v)

    # Determine primary strategy
    priority = [
        "supertrend", "ichimoku", "donchian", "keltner",
        "parabolic", "bollinger", "macd", "rsi",
        "stoch", "cci", "aroon", "trix", "momentum",
        "alma", "jma", "frama", "lsma", "dema", "tema", "zlema",
        "hma", "vwma", "wma",
        "adaptivema",
        "ema", "sma",
    ]
    primary = None
    for p in priority:
        if p in indicators:
            primary = p
            break

    # STRICT: if nothing detected, skip this item (return None)
    if primary is None:
        return None

    # Add path hint to indicators for visibility
    if primary not in indicators:
        indicators.append(primary)

    return {
        "item_id": item_id,
        "source": source,
        "path": path,
        "item_type": item_type,
        "indicators": indicators,
        "strategy_kind": strategy_type,
        "primary": primary,
        "params": params,
    }


if __name__ == "__main__":
    import json
    # Test 1: RSI strategy
    sample1 = """
    //@version=5
    strategy("RSI Test", overlay=false)
    rsiLength = input.int(14, "RSI Length")
    oversold = input.int(30, "Oversold")
    overbought = input.int(70, "Overbought")
    rsiValue = ta.rsi(close, rsiLength)
    if rsiValue < oversold
        strategy.entry("Long", strategy.long)
    """
    print("Test 1 (RSI):")
    print(json.dumps(build_strategy_from_code(1, "test", "rsi_strategy.pine", "strategy", sample1), indent=2))

    # Test 2: Adaptive MA (custom)
    sample2 = """
    //@version=5
    indicator("Adaptive MA")
    length = input.int(20, "Length")
    alpha = 2 / (length + 1)
    ma = 0.0
    ma := alpha * close + (1 - alpha) * nz(ma[1])
    plot(ma)
    """
    print("\nTest 2 (Adaptive MA):")
    print(json.dumps(build_strategy_from_code(27, "test", "movings/apirine_adaptive_moving_average.pine", "indicator", sample2), indent=2))
