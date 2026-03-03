import { exec } from 'child_process';
import { promisify } from 'util';
import path from 'path';
import { readdir } from 'fs/promises';

const execAsync = promisify(exec);

const SCRIPTS_PATH = process.env.PYTHON_SCRIPTS_PATH || './scripts';
const TRACKER_SYMBOL_DIR = process.env.TRACKER_SYMBOL_DIR || './data/tracker/symbol';

const NON_CRYPTO_TERMS = [
  'forex', 'eur/usd', 'gbp/usd', 'usd/jpy', 'eur/', 'gbp/', 'jpy',
  's&p', 's&p500', 'nasdaq', 'dow jones', 'nikkei', 'ftse',
  'crude oil', 'gold futures', 'silver futures', 'wheat', 'corn',
  'treasury', 'bond yield', 'interest rate',
];

const UNSUPPORTED_CHAINS = [
  'solana', 'arbitrum', 'optimism', 'polygon', 'avalanche', 'bsc',
  'binance', 'coinbase', 'kraken', 'okx', 'bybit', 'dydx',
  'gmx', 'uniswap', 'raydium', 'jupiter',
];

async function getTrackerSymbolCount(): Promise<number> {
  const files = (await readdir(TRACKER_SYMBOL_DIR)) as string[];
  return files.filter((name: string) => name.endsWith('.json')).length;
}

export async function executeJob(job: any): Promise<{ type: string; value: string }> {
  const req = job.requirements || {};
  const rawMax = req.max_signals ?? req.maxSignals ?? 2;
  const parsedMax = Number(rawMax);
  const maxSignals = Math.min(2, Math.max(1, Number.isFinite(parsedMax) ? parsedMax : 2));

  console.log(`[ACP] Generating best ${maxSignals} trade signals`);

  const scriptPath = path.join(SCRIPTS_PATH, 'generate_best_trade_signals.py');
  const command = `python3 ${scriptPath} --max ${maxSignals}`;

  console.log(`[ACP] Executing: ${command}`);

  const { stdout, stderr } = await execAsync(command, {
    timeout: 150000,
    maxBuffer: 1024 * 1024 * 10,
  });

  if (stderr) {
    console.error(`[ACP] stderr: ${stderr}`);
  }

  const text = stdout.trim();
  if (!text) {
    throw new Error('No output from generate_best_trade_signals.py');
  }

  console.log(`[ACP] Generated best signals (${text.length} chars)`);
  return { type: 'text', value: text };
}

export async function validateJob(_job: any): Promise<boolean> {
  const req = _job?.requirements || {};
  const maxSignalsRaw = req.max_signals ?? req.maxSignals;
  if (maxSignalsRaw !== undefined) {
    const parsed = Number(maxSignalsRaw);
    if (!Number.isFinite(parsed) || parsed < 1 || parsed > 2) {
      throw new Error('max_signals must be 1 or 2');
    }
  }

  if (req.symbol || req.symbols || req.token || req.asset) {
    throw new Error('best_trade_signals auto-selects symbols; use perp_trade_setup for a specific symbol');
  }

  const chain = String(req.chain ?? req.exchange ?? '').toLowerCase();
  if (chain && UNSUPPORTED_CHAINS.some((term) => chain.includes(term))) {
    throw new Error(`Unsupported chain/exchange '${chain}'. This service covers Hyperliquid perpetuals only.`);
  }

  const allText = Object.values(req).map((value) => String(value).toLowerCase()).join(' ');
  for (const term of UNSUPPORTED_CHAINS) {
    if (allText.includes(term)) {
      throw new Error(`Unsupported platform '${term}'. This service covers Hyperliquid perpetuals only.`);
    }
  }
  for (const term of NON_CRYPTO_TERMS) {
    if (allText.includes(term)) {
      throw new Error(`Unsupported asset class ('${term}'). This service covers crypto perpetuals only.`);
    }
  }

  const trackerCount = await getTrackerSymbolCount();
  if (trackerCount < 3) {
    throw new Error(`Insufficient market data (${trackerCount} symbols) — try again later`);
  }

  return true;
}
