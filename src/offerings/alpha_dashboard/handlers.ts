import { exec } from 'child_process';
import { promisify } from 'util';
import path from 'path';
import { readdir, stat } from 'fs/promises';

const execAsync = promisify(exec);

const SCRIPTS_PATH = process.env.PYTHON_SCRIPTS_PATH || './scripts';
const HL_TRADER_STATE_DIR = process.env.HL_TRADER_STATE_DIR || './data/hl-trader';
const MAX_CONTEXT_AGE_SEC = Number(process.env.ALPHA_MAX_CONTEXT_AGE_SEC || '900');

export async function executeJob(job: any): Promise<{ type: string; value: string }> {
  const focus = (job.requirements?.focus || '').trim();

  console.log(`[ACP] Generating alpha dashboard${focus ? ` (focus: ${focus})` : ''}`);

  const scriptPath = path.join(SCRIPTS_PATH, 'generate_alpha_dashboard.py');
  let command = `python3 ${scriptPath}`;
  if (focus) {
    // Sanitize focus input
    const safeFocus = focus.replace(/[^a-zA-Z0-9 _-]/g, '').slice(0, 50);
    command += ` --focus "${safeFocus}"`;
  }

  console.log(`[ACP] Executing: ${command}`);

  const { stdout, stderr } = await execAsync(command, {
    timeout: 90000,
    maxBuffer: 1024 * 1024 * 10,
  });

  if (stderr) {
    console.error(`[ACP] stderr: ${stderr}`);
  }

  const text = stdout.trim();
  if (!text) {
    throw new Error('No output from generate_alpha_dashboard.py');
  }

  console.log(`[ACP] Generated dashboard (${text.length} chars)`);
  return { type: 'text', value: text };
}

export async function validateJob(_job: any): Promise<boolean> {
  const req = _job?.requirements || {};
  if (req.focus !== undefined && typeof req.focus !== 'string') {
    throw new Error('focus must be a string');
  }

  const files = (await readdir(HL_TRADER_STATE_DIR)) as string[];
  const ctxFiles = files.filter(
    (name: string) => name.startsWith('hl_trader_context_') && name.endsWith('.json')
  );

  if (!ctxFiles.length) {
    throw new Error('No market context available — trader not running');
  }

  const stats = await Promise.all(
    ctxFiles.map(async (name: string) => {
      const fullPath = path.join(HL_TRADER_STATE_DIR, name);
      const info = await stat(fullPath);
      return info.mtimeMs;
    })
  );

  const latest = Math.max(...stats);
  const ageSec = Math.floor((Date.now() - latest) / 1000);
  if (ageSec > MAX_CONTEXT_AGE_SEC) {
    throw new Error(`Market context is stale (${ageSec}s old) — trader may be offline`);
  }

  return true;
}
