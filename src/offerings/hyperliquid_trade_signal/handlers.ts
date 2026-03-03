import { exec } from 'child_process';
import { promisify } from 'util';
import path from 'path';
import { readdir } from 'fs/promises';

const execAsync = promisify(exec);

const SCRIPTS_PATH = process.env.PYTHON_SCRIPTS_PATH || './scripts';
const TRACKER_SYMBOL_DIR = process.env.TRACKER_SYMBOL_DIR || './data/tracker/symbol';

async function loadSupportedSymbols(): Promise<Set<string>> {
  const files = (await readdir(TRACKER_SYMBOL_DIR)) as string[];
  const symbols = files
    .filter((name: string) => name.endsWith('.json'))
    .map((name: string) => path.basename(name, '.json').toUpperCase());
  return new Set(symbols);
}

export async function executeJob(job: any): Promise<{ type: string; value: string }> {
  const rawSymbol = job.requirements?.symbol;
  const symbol = typeof rawSymbol === 'string' ? rawSymbol.trim().toUpperCase() : '';
  if (!symbol) {
    throw new Error('Symbol is required');
  }

  console.log(`[ACP] Generating trade setup for ${symbol}`);

  const scriptPath = path.join(SCRIPTS_PATH, 'generate_symbol_setup.py');
  const command = `python3 ${scriptPath} ${symbol}`;

  console.log(`[ACP] Executing: ${command}`);

  const { stdout, stderr } = await execAsync(command, {
    timeout: 60000,
    maxBuffer: 1024 * 1024 * 10,
  });

  if (stderr) {
    console.error(`[ACP] stderr: ${stderr}`);
  }

  const text = stdout.trim();
  if (!text) {
    throw new Error(`No output from generate_symbol_setup.py for ${symbol}`);
  }

  console.log(`[ACP] Generated setup for ${symbol} (${text.length} chars)`);
  return { type: 'text', value: text };
}

export async function validateJob(job: any): Promise<boolean> {
  const symbol = job.requirements?.symbol;
  if (!symbol || typeof symbol !== 'string') {
    throw new Error('Symbol is required');
  }
  if (!/^[A-Za-z0-9]{2,10}$/.test(symbol.trim())) {
    throw new Error('Invalid symbol format');
  }
  const supported = await loadSupportedSymbols();
  if (!supported.has(symbol.trim().toUpperCase())) {
    throw new Error(`Unsupported symbol '${symbol}' (no local data)`);
  }
  return true;
}
