#!/usr/bin/env python3
"""
ACP v2 Seller Agent

Hybrid real-time + polling seller:
  - Socket.IO for instant job notifications (like the TS seller)
  - Polling fallback every 30s if socket disconnects
  - Name-based offering routing from memo content
  - Per-offering requirement validation
  - Per-service minimum pricing enforcement

Offerings:
  - perp_trade_setup    → generate_symbol_setup.py <SYMBOL>
  - best_trade_signals  → generate_best_trade_signals.py --max N
  - alpha_dashboard     → generate_alpha_dashboard.py [--focus X]

Usage:
  python seller.py
  python seller.py --interval 10   # poll fallback every 10s
  python seller.py --no-socket     # poll-only mode
"""

import os
import sys
import re
import json
import time
import random
import subprocess
import argparse
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv

load_dotenv(override=True)

try:
    from virtuals_acp.client import VirtualsACP, ACPJob, ACPJobPhase
    from virtuals_acp.contract_clients.contract_client_v2 import ACPContractClientV2
except ImportError:
    print("Install: pip install virtuals-acp python-dotenv")
    sys.exit(1)

try:
    import socketio as _socketio_mod
except ImportError:
    _socketio_mod = None
    print("Warning: python-socketio not installed — poll-only mode")
    print("  Install: pip install python-socketio websocket-client")

# --- Config ---
PRIVATE_KEY = os.getenv("WHITELISTED_WALLET_PRIVATE_KEY", "")
SELLER_WALLET = os.getenv("SELLER_AGENT_WALLET_ADDRESS", "")
SELLER_ENTITY_ID = int(os.getenv("SELLER_ENTITY_ID", "0"))

SCRIPTS_PATH = os.getenv("PYTHON_SCRIPTS_PATH", "./scripts")

ACP_SOCKET_URL = "https://acpx.virtuals.io"

# --- Offerings registry ---

OFFERINGS = {
    "perp_trade_setup": {"min_price": 0.25},
    "best_trade_signals": {"min_price": 0.25},
    "alpha_dashboard": {"min_price": 0.50},
}

# Offering aliases (TS seller uses hyperliquid_trade_signal)
OFFERING_ALIASES = {
    "hyperliquid_trade_signal": "perp_trade_setup",
}

# --- Data paths for pre-accept validation ---
TRACKER_SYMBOL_DIR = Path(os.getenv("TRACKER_SYMBOL_DIR", "./data/tracker/symbol"))
HL_TRADER_STATE_DIR = Path(os.getenv("HL_TRADER_STATE_DIR", "./data/hl-trader"))
MAX_CONTEXT_AGE_SEC = 900  # reject alpha_dashboard if context older than 15 min

# Track processed job phases to avoid reprocessing
processed: Dict[int, Dict[str, bool]] = {}
# Lock for thread-safe access when socket events fire
_lock = threading.Lock()
# Prevent two threads (socket + poll) from processing the same job simultaneously
_processing: set = set()
# Limit concurrent script/LLM executions — excess jobs queue and wait
_script_semaphore = threading.Semaphore(3)
# Serialize ALL on-chain SDK calls to prevent AA25 nonce collisions.
# Only one thread can submit a UserOp at a time.
_chain_lock = threading.Lock()
# Cache for best_trade_signals (same market scan for everyone)
_bts_cache: Dict[str, Any] = {"output": None, "ts": 0.0}
_BTS_CACHE_TTL = 300  # 5 minutes
_bts_gen_lock = threading.Lock()  # only one thread generates at a time

# Cache for alpha_dashboard (no-focus only; focused requests bypass cache)
_alpha_cache: Dict[str, Any] = {"output": None, "ts": 0.0}
_ALPHA_CACHE_TTL = 600  # 10 minutes
_alpha_gen_lock = threading.Lock()

# --- Socket health tracking ---
_disconnect_ts: List[float] = []          # timestamps of recent disconnects
_socket_rebuilds = 0                       # how many times we've rebuilt the socket
SOCKET_FLAP_THRESHOLD = 10                 # disconnects in window = flapping
SOCKET_FLAP_WINDOW = 300                   # 5 minute window
SOCKET_MAX_REBUILDS = 3                    # after N failed rebuilds, exit for watchdog
HEALTH_FILE = Path(__file__).parent / ".seller.health"


# ============================================================================
# Script runner
# ============================================================================

def run_script(cmd: list, timeout: int = 120, retries: int = 3,
               base_delay: float = 5.0, use_semaphore: bool = False) -> str:
    """Run a Python script and return stdout.

    Args:
        timeout: per-attempt subprocess timeout in seconds
        retries: max attempts
        base_delay: base retry delay (jitter added to prevent thundering herd)
        use_semaphore: if True, acquire _script_semaphore (limits concurrency)
    """
    last_err = None
    acquired = False
    try:
        if use_semaphore:
            print(f"  Waiting for script slot ({_script_semaphore._value} free)...")
            _script_semaphore.acquire()
            acquired = True
            print(f"  Slot acquired.")
        for attempt in range(1, retries + 1):
            if attempt > 1:
                delay = base_delay * attempt + random.uniform(0, base_delay)
                print(f"  Retry {attempt}/{retries} in {delay:.1f}s...")
                time.sleep(delay)
            print(f"  Exec: {' '.join(cmd)}")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0:
                text = result.stdout.strip()
                if text:
                    return text
                last_err = f"Script returned empty output | stderr={result.stderr.strip()[:200]}"
            else:
                last_err = f"Script failed (rc={result.returncode}): {result.stderr.strip()[:200] if result.stderr else 'unknown error'}"
            print(f"  Attempt {attempt} failed: {last_err}")
        raise RuntimeError(last_err)
    finally:
        if acquired:
            _script_semaphore.release()


# ============================================================================
# Offering handlers
# ============================================================================

def handle_perp_trade_setup(req: dict) -> str:
    symbol = (req.get("symbol") or "").strip().upper() or "ETH"
    script = os.path.join(SCRIPTS_PATH, "generate_symbol_setup.py")
    return run_script(["python3", script, symbol], timeout=90,
                      use_semaphore=True)


def handle_best_trade_signals(req: dict) -> str:
    max_n = req.get("max_signals", req.get("maxSignals", 2))
    max_n = min(2, max(1, int(max_n)))

    # Serve from cache if fresh (< 5 min old)
    with _lock:
        age = time.time() - _bts_cache["ts"]
        cached = _bts_cache["output"] if age < _BTS_CACHE_TTL else None
    if cached:
        print("  [cache] best_trade_signals hit (age={:.0f}s)".format(age))
        return _trim_signals(cached, max_n)

    # Cache miss — serialize generation so only ONE thread runs the script.
    # Others wait here, then re-check cache (which the first thread populated).
    with _bts_gen_lock:
        # Re-check cache — another thread may have populated it while we waited
        with _lock:
            age = time.time() - _bts_cache["ts"]
            cached = _bts_cache["output"] if age < _BTS_CACHE_TTL else None
        if cached:
            print("  [cache] best_trade_signals hit after wait (age={:.0f}s)".format(age))
            return _trim_signals(cached, max_n)

        timeout_sec = os.getenv("BEST_TRADE_TIMEOUT_SEC", "120")
        script = os.path.join(SCRIPTS_PATH, "generate_best_trade_signals.py")
        result = run_script([
            "python3", script,
            "--max", "2",
            "--candidates", "12",
            "--timeout", timeout_sec,
        ], timeout=180, use_semaphore=True)

        with _lock:
            _bts_cache["output"] = result
            _bts_cache["ts"] = time.time()
        print("  [cache] best_trade_signals populated")
        return _trim_signals(result, max_n)


def _trim_signals(text: str, max_n: int) -> str:
    """If max_n=1, trim output to just the first signal."""
    if max_n >= 2:
        return text
    # Output format: lines starting with #1, #2, etc.
    lines = text.split("\n")
    out = []
    for line in lines:
        if line.strip().startswith("#2"):
            break
        out.append(line)
    return "\n".join(out).rstrip()


def handle_alpha_dashboard(req: dict) -> str:
    script = os.path.join(SCRIPTS_PATH, "generate_alpha_dashboard.py")
    cmd = ["python3", script]
    focus = (req.get("focus") or "").strip()
    if focus:
        safe = re.sub(r'[^a-zA-Z0-9 _-]', '', focus)[:50]
        cmd += ["--focus", safe]
        # Focused requests always generate fresh (bypass cache)
        return run_script(cmd, timeout=120, retries=4, base_delay=8.0,
                          use_semaphore=True)

    # No focus — serve from cache if fresh
    with _lock:
        age = time.time() - _alpha_cache["ts"]
        cached = _alpha_cache["output"] if age < _ALPHA_CACHE_TTL else None
    if cached:
        print("  [cache] alpha_dashboard hit (age={:.0f}s)".format(age))
        return cached

    with _alpha_gen_lock:
        with _lock:
            age = time.time() - _alpha_cache["ts"]
            cached = _alpha_cache["output"] if age < _ALPHA_CACHE_TTL else None
        if cached:
            print("  [cache] alpha_dashboard hit after wait (age={:.0f}s)".format(age))
            return cached

        result = run_script(cmd, timeout=120, retries=4, base_delay=8.0,
                            use_semaphore=True)
        with _lock:
            _alpha_cache["output"] = result
            _alpha_cache["ts"] = time.time()
        print("  [cache] alpha_dashboard populated")
        return result


HANDLERS = {
    "perp_trade_setup": handle_perp_trade_setup,
    "best_trade_signals": handle_best_trade_signals,
    "alpha_dashboard": handle_alpha_dashboard,
}


# ============================================================================
# Offering validators
# ============================================================================

# Common name -> ticker aliases for fuzzy symbol resolution
_SYMBOL_ALIASES: Dict[str, str] = {
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL",
    "DOGECOIN": "DOGE", "CARDANO": "ADA", "POLKADOT": "DOT",
    "CHAINLINK": "LINK", "AVALANCHE": "AVAX", "RIPPLE": "XRP",
    "LITECOIN": "LTC", "POLYGON": "MATIC", "UNISWAP": "UNI",
    "AAVE": "AAVE", "ARBITRUM": "ARB", "OPTIMISM": "OP",
    "COSMOS": "ATOM", "NEAR": "NEAR", "APTOS": "APT",
    "CELESTIA": "TIA", "SUI": "SUI", "SEI": "SEI",
}


def _resolve_tracker_symbol(raw: str) -> Optional[str]:
    """Resolve a symbol to a tracker file, matching generate_symbol_setup.py logic."""
    sym = (raw or "").strip().upper()
    if not sym:
        return None
    # Try exact (lowercase file)
    if (TRACKER_SYMBOL_DIR / f"{sym.lower()}.json").exists():
        return sym
    # Plural: VIRTUALS -> VIRTUAL
    if sym.endswith("S") and (TRACKER_SYMBOL_DIR / f"{sym[:-1].lower()}.json").exists():
        return sym[:-1]
    # Strip common suffixes: BTC-PERP -> BTC
    for suf in ("-PERP", "PERP"):
        if sym.endswith(suf):
            base = sym[:-len(suf)]
            if base and (TRACKER_SYMBOL_DIR / f"{base.lower()}.json").exists():
                return base
    # Common name aliases: BITCOIN -> BTC, ETHEREUM -> ETH, etc.
    aliased = _SYMBOL_ALIASES.get(sym)
    if aliased and (TRACKER_SYMBOL_DIR / f"{aliased.lower()}.json").exists():
        return aliased
    return None


def _list_supported_symbols() -> List[str]:
    """Return sorted list of symbols we have tracker data for."""
    return sorted(p.stem.upper() for p in TRACKER_SYMBOL_DIR.glob("*.json"))


def _normalize_offering_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    key = str(name).strip().lower()
    if not key:
        return None
    return OFFERING_ALIASES.get(key, key)


def validate_perp_trade_setup(req: dict) -> Tuple[bool, Optional[str]]:
    symbol = (req.get("symbol") or "").strip()
    if not symbol:
        return False, "symbol is required"
    if len(symbol) > 20:
        return False, "symbol too long"
    resolved = _resolve_tracker_symbol(symbol)
    if not resolved:
        return False, f"Symbol '{symbol.upper()}' not supported — no tracker data available"
    return True, None


# Non-crypto terms that should trigger rejection
_NON_CRYPTO_TERMS = [
    "forex", "eur/usd", "gbp/usd", "usd/jpy", "eur/", "gbp/", "jpy",
    "s&p", "s&p500", "nasdaq", "dow jones", "nikkei", "ftse",
    "crude oil", "gold futures", "silver futures", "wheat", "corn",
    "treasury", "bond yield", "interest rate",
]

# Unsupported chains/exchanges
_UNSUPPORTED_CHAINS = [
    "solana", "arbitrum", "optimism", "polygon", "avalanche", "bsc",
    "binance", "coinbase", "kraken", "okx", "bybit", "dydx",
    "gmx", "uniswap", "raydium", "jupiter",
]

# NSFW / policy-violating keywords — reject at REQUEST phase
_NSFW_BLOCKLIST = [
    "nsfw", "sexual", "porn", "xxx", "nude", "naked", "hentai",
    "violence", "gore", "murder", "kill", "torture", "abuse",
    "child", "minor", "underage", "illegal", "drug", "narcotic",
    "terrorism", "bomb", "weapon", "hate speech", "racist",
    "self-harm", "suicide",
]

# Valid alpha_dashboard focus topics (crypto concepts the dashboard can tilt toward)
_VALID_FOCUS_TOPICS = [
    "eth", "btc", "sol", "doge", "avax", "link", "sui", "matic",
    "arb", "op", "ada", "dot", "atom", "near", "apt", "sei",
    "tia", "jup", "wif", "bonk", "pepe", "shib", "floki",
    "hype", "virtual", "trump", "melania", "anime", "turbo",
    "memecoins", "memes", "memecoin", "meme coins", "meme",
    "funding", "funding rates", "funding rate",
    "whale", "whale activity", "whales", "whale flow",
    "liquidation", "liquidations", "liq",
    "volatility", "vol", "implied vol", "realized vol",
    "positioning", "open interest", "oi",
    "altcoins", "alts", "alt coins",
    "large caps", "large cap", "small caps", "small cap", "mid cap",
    "defi", "ai", "ai tokens", "gaming", "l1", "l2",
    "shorts", "longs", "sentiment", "momentum", "trend",
    "bearish", "bullish", "market overview", "overview",
]


def validate_best_trade_signals(req: dict) -> Tuple[bool, Optional[str]]:
    # NSFW / policy check on all input values
    all_text = " ".join(str(v) for v in req.values()).lower()
    if _is_nsfw(all_text):
        return False, "Request rejected: content violates safety policy"
    max_s = req.get("max_signals", req.get("maxSignals"))
    if max_s is not None:
        try:
            n = int(max_s)
            # Clamp to [1,2] — don't reject, just normalize
            req["max_signals"] = max(1, min(2, n))
        except (ValueError, TypeError):
            # Non-numeric value — default to 2 instead of rejecting
            req["max_signals"] = 2
    # Reject if buyer specifies a symbol — wrong offering
    if req.get("symbol") or req.get("symbols") or req.get("token") or req.get("asset"):
        return False, "best_trade_signals auto-selects the best opportunities. Use perp_trade_setup for a specific symbol."
    # Reject unsupported chains/exchanges
    chain = str(req.get("chain", req.get("exchange", ""))).lower()
    if chain and any(c in chain for c in _UNSUPPORTED_CHAINS):
        return False, f"Unsupported chain/exchange '{chain}'. This service covers Hyperliquid perpetuals only."
    # Check all string values for non-crypto or unsupported chain mentions
    all_text = " ".join(str(v) for v in req.values()).lower()
    for term in _UNSUPPORTED_CHAINS:
        if term in all_text:
            return False, f"Unsupported platform '{term}'. This service covers Hyperliquid perpetuals only."
    for term in _NON_CRYPTO_TERMS:
        if term in all_text:
            return False, f"Unsupported asset class ('{term}'). This service covers crypto perpetuals only."
    # Check tracker dir has enough data to scan
    tracker_count = len(list(TRACKER_SYMBOL_DIR.glob("*.json")))
    if tracker_count < 3:
        return False, f"Insufficient market data ({tracker_count} symbols) — try again later"
    return True, None


def _is_nsfw(text: str) -> bool:
    """Check if text contains NSFW or policy-violating content."""
    lower = text.lower()
    return any(term in lower for term in _NSFW_BLOCKLIST)


def _is_valid_focus(focus: str) -> bool:
    """Check if focus is a recognized crypto topic or tracker symbol."""
    lower = focus.lower().strip()
    if not lower:
        return True  # empty focus is valid (optional field)
    # Match against known focus topics
    if lower in _VALID_FOCUS_TOPICS or any(topic in lower for topic in _VALID_FOCUS_TOPICS):
        return True
    # Match against tracker symbols (e.g. "ETH", "BTC")
    resolved = _resolve_tracker_symbol(focus)
    if resolved:
        return True
    return False


def validate_alpha_dashboard(req: dict) -> Tuple[bool, Optional[str]]:
    focus = req.get("focus")
    if focus is not None and not isinstance(focus, str):
        return False, "focus must be a string"
    if focus:
        focus_lower = focus.lower()
        # NSFW / policy-violating content — reject immediately
        if _is_nsfw(focus):
            return False, f"Request rejected: content violates safety policy"
        # Reject non-crypto focus topics
        for term in _NON_CRYPTO_TERMS:
            if term in focus_lower:
                return False, f"Unsupported focus topic ('{term}'). Alpha dashboard covers crypto perpetuals on Hyperliquid only."
        for term in _UNSUPPORTED_CHAINS:
            if term in focus_lower:
                return False, f"Unsupported platform '{term}'. Alpha dashboard covers Hyperliquid perpetuals only."
        # Validate focus is a recognized crypto topic or symbol
        if not _is_valid_focus(focus):
            return False, f"Unsupported focus area '{focus}'. Supported: crypto symbols (ETH, BTC, SOL...), or topics like memecoins, funding rates, whale activity, volatility, liquidations."
    # Check hl-trader context exists and is fresh enough
    ctx_files = sorted(HL_TRADER_STATE_DIR.glob("hl_trader_context_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not ctx_files:
        return False, "No market context available — trader not running"
    age = time.time() - ctx_files[0].stat().st_mtime
    if age > MAX_CONTEXT_AGE_SEC:
        return False, f"Market context is stale ({int(age)}s old) — trader may be offline"
    return True, None


VALIDATORS = {
    "perp_trade_setup": validate_perp_trade_setup,
    "best_trade_signals": validate_best_trade_signals,
    "alpha_dashboard": validate_alpha_dashboard,
}


# ============================================================================
# Memo parsing — extract offering name + requirements from job
# ============================================================================

def parse_offering_and_req(job: ACPJob) -> Tuple[Optional[str], dict]:
    """Extract offering name and requirements from job memos.

    Buyer sends: {"name": "perp_trade_setup", "requirement": {"symbol": "ETH"}}
    This is stored in memos[0].content.
    """
    for memo in job.memos:
        if not memo.content:
            continue
        try:
            data = json.loads(str(memo.content)) if isinstance(memo.content, str) else memo.content
            if isinstance(data, dict) and "name" in data:
                name = _normalize_offering_name(data["name"])
                req = data.get("requirement", {})
                if not isinstance(req, dict):
                    req = {}
                return name, req
        except (json.JSONDecodeError, TypeError):
            continue

    # No structured memo found — memo may not be indexed yet.
    # Return None so the job is skipped and retried on the next cycle.
    return None, {}


# ============================================================================
# Job processing (shared by socket events and polling)
# ============================================================================

def process_single_job(acp: VirtualsACP, job_id: int):
    """Process a single job by ID. Thread-safe."""
    with _lock:
        if job_id in _processing:
            return  # another thread is already handling this job
        stages = processed.get(job_id, {})
        if stages.get("responded") and stages.get("delivered"):
            return
        _processing.add(job_id)
        # Snapshot to avoid double-processing
        already_responded = stages.get("responded", False)
        already_delivered = stages.get("delivered", False)

    try:
        detail = acp.get_job_by_onchain_id(job_id)
        phase = detail.phase

        # REQUEST → validate, accept or reject
        if phase == ACPJobPhase.REQUEST and not already_responded:
            offering_name, req = parse_offering_and_req(detail)

            # Memo not indexed yet — skip, will retry next cycle
            if offering_name is None:
                return

            print(f"  [{job_id}] REQUEST  offering={offering_name}  price={detail.price}")

            # Check offering exists
            if offering_name not in OFFERINGS:
                print(f"  [{job_id}] REJECT: unknown offering '{offering_name}'")
                with _chain_lock:
                    detail.respond(False, f"Invalid offering: {offering_name}")
                with _lock:
                    processed.setdefault(job_id, {})["responded"] = True
                return

            # Check price
            min_price = OFFERINGS[offering_name]["min_price"]
            if detail.price < min_price:
                print(f"  [{job_id}] REJECT: price {detail.price} < ${min_price} for {offering_name}")
                with _chain_lock:
                    detail.respond(False, f"Price below minimum for {offering_name} (${min_price})")
                with _lock:
                    processed.setdefault(job_id, {})["responded"] = True
                return

            # Validate requirements
            validator = VALIDATORS.get(offering_name)
            if validator:
                valid, reason = validator(req)
                if not valid:
                    print(f"  [{job_id}] REJECT: validation failed — {reason}")
                    with _chain_lock:
                        detail.respond(False, f"Validation failed: {reason}")
                    with _lock:
                        processed.setdefault(job_id, {})["responded"] = True
                    return

            # Accept
            print(f"  [{job_id}] ACCEPT  {offering_name} @ ${detail.price}")
            with _chain_lock:
                detail.respond(True, f"Ready to provide {offering_name}")
            # Verify accept landed — Alchemy can silently drop UserOps under load.
            # respond(True) = accept() → NEGOTIATION, then create_requirement() → TRANSACTION.
            # The ACP API can return optimistic/cached phase, so we check BOTH
            # phase AND memo count. Need >= 2 memos for buyer to pay.
            time.sleep(4)
            check = acp.get_job_by_onchain_id(job_id)
            n_memos = len(check.memos)
            print(f"  [{job_id}] verify: phase={check.phase} memos={n_memos}")
            if n_memos >= 2:
                pass  # Both accept + create_requirement landed
            elif check.phase in (ACPJobPhase.NEGOTIATION, ACPJobPhase.TRANSACTION) and n_memos < 2:
                # accept() landed but create_requirement() was dropped
                print(f"  [{job_id}] WARN: only {n_memos} memo(s), retrying create_requirement...")
                time.sleep(2)
                check = acp.get_job_by_onchain_id(job_id)
                if len(check.memos) < 2:
                    try:
                        with _chain_lock:
                            check.create_requirement(f"Ready to provide {offering_name}")
                        print(f"  [{job_id}] create_requirement retry sent")
                    except Exception as retry_err:
                        print(f"  [{job_id}] create_requirement retry failed: {retry_err}")
                else:
                    print(f"  [{job_id}] memo landed on recheck")
            elif check.phase == ACPJobPhase.REQUEST:
                # Neither op landed — retry full respond
                print(f"  [{job_id}] WARN: still at REQUEST, retrying full respond...")
                time.sleep(2)
                try:
                    check = acp.get_job_by_onchain_id(job_id)
                    with _chain_lock:
                        check.respond(True, f"Ready to provide {offering_name}")
                    print(f"  [{job_id}] Full retry sent")
                except Exception as retry_err:
                    print(f"  [{job_id}] Full retry failed: {retry_err}")
            with _lock:
                processed.setdefault(job_id, {})["responded"] = True

        # TRANSACTION → generate and deliver
        elif phase == ACPJobPhase.TRANSACTION and not already_delivered:
            offering_name, req = parse_offering_and_req(detail)
            handler = HANDLERS.get(offering_name)

            if not handler:
                print(f"  [{job_id}] TRANSACTION but no handler for '{offering_name}'")
                with _chain_lock:
                    detail.deliver(f"Error: unknown offering {offering_name}")
                with _lock:
                    processed.setdefault(job_id, {})["delivered"] = True
                return

            print(f"  [{job_id}] TRANSACTION  generating {offering_name}...")
            # Step 1: Run the handler (script). Errors here = real script failure.
            text = None
            try:
                text = handler(req)
                if not text:
                    text = "No data available. Please try again later."
            except Exception as e:
                print(f"  [{job_id}] Script error: {e}")
                try:
                    with _chain_lock:
                        detail.reject(f"Temporary service error for {offering_name}. Please retry.")
                    print(f"  [{job_id}] REJECTED (script failure)")
                except Exception as rej_err:
                    print(f"  [{job_id}] Failed to reject after script error: {rej_err}")

            # Step 2: Deliver the result. If deliver() throws (e.g. AA25 nonce),
            # check on-chain state before assuming failure — the tx may have landed.
            if text is not None:
                try:
                    with _chain_lock:
                        detail.deliver(text)
                    print(f"  [{job_id}] DELIVERED  ({len(text)} chars)")
                except Exception as e:
                    print(f"  [{job_id}] Deliver error: {e}")
                    # Re-fetch job to see if delivery actually landed on-chain
                    time.sleep(3)
                    try:
                        recheck = acp.get_job_by_onchain_id(job_id)
                        rc_phase = recheck.phase
                        if rc_phase in (ACPJobPhase.EVALUATION, ACPJobPhase.COMPLETED):
                            print(f"  [{job_id}] Delivery landed on-chain despite error (phase={rc_phase})")
                        else:
                            print(f"  [{job_id}] Delivery NOT on-chain (phase={rc_phase}), rejecting...")
                            try:
                                with _chain_lock:
                                    recheck.reject(f"Temporary service error for {offering_name}. Please retry.")
                                print(f"  [{job_id}] REJECTED (deliver failed)")
                            except Exception as rej_err:
                                print(f"  [{job_id}] Failed to reject after deliver error: {rej_err}")
                    except Exception as check_err:
                        print(f"  [{job_id}] Could not recheck job state: {check_err}")

            with _lock:
                processed.setdefault(job_id, {})["delivered"] = True

        # Terminal phases
        elif phase in (ACPJobPhase.EVALUATION, ACPJobPhase.COMPLETED, ACPJobPhase.REJECTED):
            with _lock:
                s = processed.setdefault(job_id, {})
                if not s.get("responded"):
                    print(f"  [{job_id}] {phase.name}")
                s["responded"] = True
                s["delivered"] = True

    except Exception as e:
        print(f"  [{job_id}] Error: {e}")
    finally:
        with _lock:
            _processing.discard(job_id)


def poll_jobs(acp: VirtualsACP):
    """Single poll cycle: check active jobs and handle each phase."""
    jobs = acp.get_active_jobs()
    if not jobs:
        return

    for job in jobs:
        if job.provider_address and job.provider_address.lower() != acp.agent_address.lower():
            continue
        process_single_job(acp, job.id)


# ============================================================================
# Socket.IO — real-time job notifications
# ============================================================================

def _record_disconnect():
    """Record a disconnect timestamp for flap detection."""
    global _disconnect_ts
    now = time.time()
    _disconnect_ts.append(now)
    # Prune old entries outside window
    cutoff = now - SOCKET_FLAP_WINDOW
    _disconnect_ts = [t for t in _disconnect_ts if t > cutoff]


def _is_socket_flapping() -> bool:
    """Return True if disconnect count in window exceeds threshold."""
    cutoff = time.time() - SOCKET_FLAP_WINDOW
    recent = [t for t in _disconnect_ts if t > cutoff]
    return len(recent) >= SOCKET_FLAP_THRESHOLD


def setup_socket(acp: VirtualsACP, wallet: str):
    """Connect Socket.IO for real-time job events. Returns client or None."""
    if _socketio_mod is None:
        return None

    sio = _socketio_mod.Client(
        reconnection=True,
        reconnection_attempts=0,  # retry forever
        reconnection_delay=5,
        reconnection_delay_max=30,
        logger=False,
    )

    @sio.on('connect')
    def on_connect():
        print("[socket] Connected to ACP")

    @sio.on('disconnect')
    def on_disconnect():
        _record_disconnect()
        n_recent = len([t for t in _disconnect_ts if t > time.time() - SOCKET_FLAP_WINDOW])
        print(f"[socket] Disconnected (recent={n_recent}/{SOCKET_FLAP_THRESHOLD})")

    @sio.on('connect_error')
    def on_connect_error(data):
        print(f"[socket] Connection error: {data}")

    @sio.on('roomJoined')
    def on_room_joined(*args):
        print("[socket] Joined ACP room")
        return True  # ack

    @sio.on('onNewTask')
    def on_new_task(data, *args):
        job_id = data.get('id') if isinstance(data, dict) else None
        phase = data.get('phase', '?') if isinstance(data, dict) else '?'
        print(f"\n[socket] onNewTask  jobId={job_id}  phase={phase}")
        if job_id:
            t = threading.Thread(
                target=process_single_job, args=(acp, job_id), daemon=True
            )
            t.start()
        return True  # ack

    @sio.on('onEvaluate')
    def on_evaluate(data, *args):
        job_id = data.get('id') if isinstance(data, dict) else None
        print(f"[socket] onEvaluate  jobId={job_id}")
        return True  # ack

    try:
        sio.connect(
            ACP_SOCKET_URL,
            auth={'walletAddress': wallet},
            transports=['websocket'],
        )
        return sio
    except Exception as e:
        print(f"[socket] Failed to connect: {e}")
        print("[socket] Falling back to poll-only mode")
        return None


def teardown_socket(sio) -> None:
    """Forcefully disconnect and clean up a socket client."""
    if sio is None:
        return
    try:
        if sio.connected:
            sio.disconnect()
    except Exception:
        pass


# ============================================================================
# Main
# ============================================================================

def _write_health(sio, poll_count: int):
    """Write health file for watchdog to inspect."""
    try:
        now = time.time()
        cutoff = now - SOCKET_FLAP_WINDOW
        recent_dc = len([t for t in _disconnect_ts if t > cutoff])
        data = {
            "pid": os.getpid(),
            "ts": int(now),
            "poll_count": poll_count,
            "socket_connected": bool(sio and sio.connected),
            "recent_disconnects": recent_dc,
            "rebuilds": _socket_rebuilds,
        }
        HEALTH_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def main():
    global _socket_rebuilds, _disconnect_ts

    parser = argparse.ArgumentParser(description="ACP Seller Agent")
    parser.add_argument("--interval", type=int, default=15,
                        help="Poll fallback interval in seconds (default 15)")
    parser.add_argument("--no-socket", action="store_true",
                        help="Disable Socket.IO, use polling only")
    args = parser.parse_args()

    # Write health file immediately so watchdog doesn't kill us before first poll
    _write_health(None, 0)

    print(f"Seller:    {SELLER_WALLET}")
    print(f"Entity:    {SELLER_ENTITY_ID}")
    print(f"Scripts:   {SCRIPTS_PATH}")
    print(f"Offerings:")
    for name, cfg in OFFERINGS.items():
        print(f"  - {name} (${cfg['min_price']})")

    acp = VirtualsACP(
        acp_contract_clients=ACPContractClientV2(
            wallet_private_key=PRIVATE_KEY,
            agent_wallet_address=SELLER_WALLET,
            entity_id=SELLER_ENTITY_ID,
        ),
    )
    print(f"Agent:     {acp.agent_address}")

    # Socket.IO for real-time events
    use_socket = not args.no_socket
    sio = None
    if use_socket:
        sio = setup_socket(acp, SELLER_WALLET)

    if sio and sio.connected:
        print(f"\nMode: Socket.IO (real-time) + poll fallback every {args.interval}s")
    else:
        print(f"\nMode: Poll-only every {args.interval}s")

    print("Waiting for jobs... Ctrl+C to stop\n")

    poll_count = 0
    try:
        while True:
            # --- Poll ---
            try:
                poll_jobs(acp)
            except Exception as e:
                print(f"Poll error: {e}")
            poll_count += 1

            # --- Socket health check ---
            if use_socket and _is_socket_flapping():
                print(f"[socket] FLAPPING detected ({SOCKET_FLAP_THRESHOLD} disconnects in {SOCKET_FLAP_WINDOW}s)")
                _socket_rebuilds += 1

                if _socket_rebuilds > SOCKET_MAX_REBUILDS:
                    print(f"[socket] {_socket_rebuilds} rebuilds failed — exiting for watchdog restart")
                    _write_health(sio, poll_count)
                    sys.exit(1)

                print(f"[socket] Rebuild #{_socket_rebuilds}/{SOCKET_MAX_REBUILDS} — tearing down...")
                teardown_socket(sio)
                sio = None
                _disconnect_ts.clear()

                # Backoff before rebuild: 15s * rebuild count
                backoff = 15 * _socket_rebuilds
                print(f"[socket] Waiting {backoff}s before reconnect...")
                time.sleep(backoff)

                sio = setup_socket(acp, SELLER_WALLET)
                if sio and sio.connected:
                    print(f"[socket] Rebuild #{_socket_rebuilds} succeeded")
                else:
                    print(f"[socket] Rebuild #{_socket_rebuilds} failed — poll-only until next check")

            # --- Reset rebuild counter if socket is stable ---
            if use_socket and sio and sio.connected and not _is_socket_flapping():
                if _socket_rebuilds > 0:
                    print(f"[socket] Stable — resetting rebuild counter")
                _socket_rebuilds = 0

            # --- Health file (every cycle so watchdog always sees fresh data) ---
            _write_health(sio, poll_count)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopping...")
        teardown_socket(sio)
        print("Stopped.")


if __name__ == "__main__":
    main()
