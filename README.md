# Seller Agent

Reference implementation of a Virtuals ACP seller that monetizes on-chain perp analysis through the Agent Commerce Protocol. Connects to Hyperliquid for live pricing and wraps locally-generated analysis into paid ACP offerings.

## What It Sells

| Service | What the buyer gets | Cost |
|---|---|---|
| `perp_trade_setup` | Single-symbol directional view with levels (entry zone, stop, targets) and a short reasoning blurb | 0.25 USDC |
| `best_trade_signals` | Automated scan picking the strongest 1–2 opportunities from all monitored symbols | 0.25 USDC |
| `alpha_dashboard` | High-level market snapshot with bias direction, key catalysts, and watchlist | 0.50 USDC |

## How It Works

1. **CLI layer** (`bin/acp.ts`) — registers services on the ACP network, manages the local resource server, and controls the seller process.
2. **Seller runtime** (`seller-agent/seller.py`) — listens for incoming ACP jobs over Socket.IO (with a polling fallback), validates each request, shells out to Python analysis scripts, and posts deliverables back on-chain.
3. **Resource server** (`src/lib/resource-server.ts`) — lightweight HTTP API exposing free-tier data (supported symbols, market pulse, context health) so prospective buyers can check availability before purchasing.
4. **Watchdog** (`seller-agent/watchdog.sh`) — process supervisor with crash detection, backoff, health-file monitoring, and optional tmux daemon mode.

## Requirements

- Node 18+ and Python 3.10+
- A registered Virtuals ACP entity with wallet credentials
- The companion analysis scripts (see `alpha-signals-backend`)
- Symbol tracker JSON data and trader context files

## Quick Start

```bash
npm install
pip install -r seller-agent/requirements.txt
cp .env.example .env   # fill in credentials and data paths
```

Register services, then start:

```bash
npm run acp -- sell create perp_trade_setup
npm run acp -- sell create best_trade_signals
npm run acp -- sell create alpha_dashboard

npm run acp -- resources start          # local data API
cd seller-agent && bash watchdog.sh     # production mode
```

## Free Data Endpoints

Default bind: `127.0.0.1:18791` (reverse-proxy for public access).

| Path | Returns |
|---|---|
| `/resources/get_supported_perp_trading_token_list` | Monitored symbols with exchange metadata |
| `/resources/is_symbol_supported?symbol=BTC` | Per-symbol availability check |
| `/resources/get_market_pulse` | Lightweight bias summary (no LLM) |
| `/resources/alpha_context_status` | Freshness of the latest context snapshot |
| `/resources/agent_status` | Concurrency slots and current load |

## Configuration

All settings live in `.env`. See `.env.example` for defaults and descriptions.

## License

MIT
