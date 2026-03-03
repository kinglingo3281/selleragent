import http from 'http';
import { readdir, readFile, stat } from 'fs/promises';
import path from 'path';

const DEFAULT_HOST = process.env.RESOURCE_HOST || '127.0.0.1';
const DEFAULT_PORT = Number(process.env.RESOURCE_PORT || '18791');

const TRACKER_SYMBOL_DIR = process.env.TRACKER_SYMBOL_DIR || './data/tracker/symbol';
const HL_TRADER_STATE_DIR = process.env.HL_TRADER_STATE_DIR || './data/hl-trader';
const MAX_CONTEXT_AGE_SEC = Number(process.env.RESOURCE_MAX_CONTEXT_AGE_SEC || '900');
const MAX_CONCURRENT_JOBS = Number(process.env.RESOURCE_MAX_CONCURRENT || '10');
const HL_META_URL = 'https://api.hyperliquid.xyz/info';
const HL_META_CACHE_TTL_MS = 60_000; // cache HL meta for 60s

interface HLUniverseEntry {
  name: string;
  maxLeverage: number;
  szDecimals: number;
  marginTableId: number;
}

let hlMetaCache: { data: HLUniverseEntry[]; fetchedAt: number } | null = null;

async function fetchHLMeta(): Promise<HLUniverseEntry[]> {
  if (hlMetaCache && Date.now() - hlMetaCache.fetchedAt < HL_META_CACHE_TTL_MS) {
    return hlMetaCache.data;
  }
  const resp = await fetch(HL_META_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'meta' }),
  });
  if (!resp.ok) throw new Error(`HL meta API returned ${resp.status}`);
  const body = await resp.json() as { universe: HLUniverseEntry[] };
  hlMetaCache = { data: body.universe, fetchedAt: Date.now() };
  return body.universe;
}

function sendJson(res: http.ServerResponse, status: number, body: unknown) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(body, null, 2));
}

async function getTrackerSymbols(): Promise<Set<string>> {
  const files = (await readdir(TRACKER_SYMBOL_DIR)) as string[];
  return new Set(
    files
      .filter((name: string) => name.endsWith('.json'))
      .map((name: string) => path.basename(name, '.json').toUpperCase())
  );
}

async function getSupportedPerpTradingTokenList() {
  const [universe, trackerSymbols] = await Promise.all([
    fetchHLMeta(),
    getTrackerSymbols(),
  ]);
  const now = new Date().toISOString();
  return universe
    .filter((entry: HLUniverseEntry) => trackerSymbols.has(entry.name.toUpperCase()))
    .map((entry: HLUniverseEntry) => ({
      symbol: `${entry.name.toUpperCase()}-PERP`,
      isTradable: true,
      maxLeverage: entry.maxLeverage,
      checkedAt: now,
    }))
    .sort((a: { symbol: string }, b: { symbol: string }) => a.symbol.localeCompare(b.symbol));
}

async function isSymbolSupported(rawSymbol: string) {
  const coin = rawSymbol.replace(/-PERP$/i, '').toUpperCase();
  const [universe, trackerSymbols] = await Promise.all([
    fetchHLMeta(),
    getTrackerSymbols(),
  ]);
  const hlEntry = universe.find((e: HLUniverseEntry) => e.name.toUpperCase() === coin);
  const hasTracker = trackerSymbols.has(coin);
  const now = new Date().toISOString();

  if (hlEntry && hasTracker) {
    return {
      symbol: `${coin}-PERP`,
      isTradable: true,
      maxLeverage: hlEntry.maxLeverage,
      checkedAt: now,
    };
  }

  let reasonCode = 'UNSUPPORTED';
  if (hlEntry && !hasTracker) reasonCode = 'NO_TRACKER_DATA';
  if (!hlEntry && hasTracker) reasonCode = 'NOT_ON_HYPERLIQUID';
  if (!hlEntry && !hasTracker) reasonCode = 'UNKNOWN_SYMBOL';

  return {
    symbol: `${coin}-PERP`,
    isTradable: false,
    reasonCode,
    checkedAt: now,
  };
}

type ContextStat = { name: string; mtimeMs: number };

async function getAlphaContextStatus() {
  try {
    const files = (await readdir(HL_TRADER_STATE_DIR)) as string[];
    const ctxFiles = files.filter(
      (name: string) => name.startsWith('hl_trader_context_') && name.endsWith('.json')
    );

    if (!ctxFiles.length) {
      return {
        ok: false,
        reason: 'no_context_files',
        last_context_ts: null,
        age_sec: null,
        max_age_sec: MAX_CONTEXT_AGE_SEC,
      };
    }

    const stats: ContextStat[] = await Promise.all(
      ctxFiles.map(async (name: string) => {
        const fullPath = path.join(HL_TRADER_STATE_DIR, name);
        const info = await stat(fullPath);
        return { name, mtimeMs: info.mtimeMs };
      })
    );

    stats.sort((a: ContextStat, b: ContextStat) => b.mtimeMs - a.mtimeMs);
    const latest = stats[0];
    const ageSec = Math.floor((Date.now() - latest.mtimeMs) / 1000);

    return {
      ok: ageSec <= MAX_CONTEXT_AGE_SEC,
      last_context_ts: new Date(latest.mtimeMs).toISOString(),
      age_sec: ageSec,
      max_age_sec: MAX_CONTEXT_AGE_SEC,
    };
  } catch (error: any) {
    return {
      ok: false,
      reason: error?.message || 'failed_to_read_context_dir',
      last_context_ts: null,
      age_sec: null,
      max_age_sec: MAX_CONTEXT_AGE_SEC,
    };
  }
}

interface MarketPulseCache {
  data: unknown;
  ts: number;
}

let marketPulseCache: MarketPulseCache | null = null;
const MARKET_PULSE_CACHE_TTL_MS = 120_000; // 2 min cache

async function getMarketPulse() {
  if (marketPulseCache && Date.now() - marketPulseCache.ts < MARKET_PULSE_CACHE_TTL_MS) {
    return marketPulseCache.data;
  }

  const files = (await readdir(HL_TRADER_STATE_DIR)) as string[];

  // --- Read latest context file ---
  const ctxFiles = files
    .filter((n: string) => n.startsWith('hl_trader_context_') && n.endsWith('.json'))
    .sort();
  if (!ctxFiles.length) {
    return { ok: false, reason: 'no_context_files' };
  }
  const ctxPath = path.join(HL_TRADER_STATE_DIR, ctxFiles[ctxFiles.length - 1]);
  const ctxInfo = await stat(ctxPath);
  const ctxAgeSec = Math.floor((Date.now() - ctxInfo.mtimeMs) / 1000);
  const ctx = JSON.parse(await readFile(ctxPath, 'utf-8'));

  // --- Derive bias + top tickers from selected_opportunities ---
  const opps: any[] = ctx.selected_opportunities || [];
  let longCount = 0, shortCount = 0, mixedCount = 0;
  const bullish: { symbol: string; score: number }[] = [];
  const bearish: { symbol: string; score: number }[] = [];

  for (const o of opps) {
    if (!o || typeof o !== 'object') continue;
    const dir = ((o.direction || '') as string).toUpperCase();
    if (dir === 'LONG') longCount++;
    else if (dir === 'SHORT') shortCount++;
    else mixedCount++;

    const sym = ((o.symbol || '') as string).toUpperCase();
    const score = Number(o.score || 0);
    if (dir === 'LONG') bullish.push({ symbol: sym, score });
    if (dir === 'SHORT') bearish.push({ symbol: sym, score });
  }

  bullish.sort((a, b) => b.score - a.score);
  bearish.sort((a, b) => b.score - a.score);

  let bias = 'NEUTRAL';
  if (longCount > shortCount * 1.5 && longCount > mixedCount) bias = 'LONG';
  else if (shortCount > longCount * 1.5 && shortCount > mixedCount) bias = 'SHORT';

  // --- IV summary from global_context_compact ---
  const gcc: string = ctx.global_context_compact || '';
  const ivMatch = gcc.match(/IV:.*?\|.*?\|/);
  const ivSummary = ivMatch ? ivMatch[0].replace(/\|\s*$/, '').trim() : null;

  const result = {
    bias,
    top_bullish: bullish.slice(0, 5).map(s => s.symbol),
    top_bearish: bearish.slice(0, 5).map(s => s.symbol),
    iv_summary: ivSummary,
    markets_tracked: ctx.symbol_count || opps.length || 0,
    updated_at: ctx.generated_at || new Date(ctxInfo.mtimeMs).toISOString(),
  };

  marketPulseCache = { data: result, ts: Date.now() };
  return result;
}

function getAgentStatus() {
  const currentLoadRaw = process.env.RESOURCE_CURRENT_LOAD;
  const currentLoad = currentLoadRaw ? Number(currentLoadRaw) : 0;
  const availableSlots = Math.max(0, MAX_CONCURRENT_JOBS - currentLoad);

  return {
    max_concurrent_jobs: MAX_CONCURRENT_JOBS,
    current_load: currentLoad,
    available_slots: availableSlots,
    updated_at: new Date().toISOString(),
  };
}

export async function startResourceServer(options: { host?: string; port?: number } = {}) {
  const host = options.host || DEFAULT_HOST;
  const port = options.port || DEFAULT_PORT;

  const server = http.createServer(async (req: http.IncomingMessage, res: http.ServerResponse) => {
    const url = new URL(req.url || '/', `http://${host}:${port}`);

    if (req.method !== 'GET') {
      sendJson(res, 405, { ok: false, error: 'method_not_allowed' });
      return;
    }

    try {
      if (url.pathname === '/' || url.pathname === '/resources') {
        sendJson(res, 200, {
          ok: true,
          service: 'acp-resources',
          endpoints: [
            '/resources/get_supported_perp_trading_token_list',
            '/resources/is_symbol_supported',
            '/resources/agent_status',
            '/resources/alpha_context_status',
            '/resources/get_market_pulse',
          ],
        });
        return;
      }

      if (url.pathname === '/resources/get_supported_perp_trading_token_list') {
        sendJson(res, 200, await getSupportedPerpTradingTokenList());
        return;
      }

      if (url.pathname === '/resources/is_symbol_supported') {
        const symbol = url.searchParams.get('symbol');
        if (!symbol) {
          sendJson(res, 400, { error: 'missing_symbol_param', example: '?symbol=ETH-PERP or ?symbol=ETH' });
          return;
        }
        sendJson(res, 200, await isSymbolSupported(symbol));
        return;
      }

      if (url.pathname === '/resources/agent_status') {
        sendJson(res, 200, getAgentStatus());
        return;
      }

      if (url.pathname === '/resources/alpha_context_status') {
        sendJson(res, 200, await getAlphaContextStatus());
        return;
      }

      if (url.pathname === '/resources/get_market_pulse') {
        sendJson(res, 200, await getMarketPulse());
        return;
      }

      sendJson(res, 404, { ok: false, error: 'not_found' });
    } catch (error: any) {
      sendJson(res, 500, { ok: false, error: error?.message || 'server_error' });
    }
  });

  server.listen(port, host, () => {
    console.log(`[ACP] Resource server listening on http://${host}:${port}`);
  });
}
