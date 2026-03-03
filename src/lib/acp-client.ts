import { ACPClient as VirtualsACPClient } from '@virtuals-protocol/acp-node';
import dotenv from 'dotenv';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

dotenv.config({ path: path.join(__dirname, '../../.env') });

export interface ACPConfig {
  entityId: string;
  walletAddress: string;
  privateKey: string;
  acpApiKey: string;
  gameApiKey: string;
}

export function getACPConfig(): ACPConfig {
  const config: ACPConfig = {
    entityId: process.env.AGENT_ENTITY_ID || '',
    walletAddress: process.env.AGENT_WALLET_ADDRESS || '',
    privateKey: process.env.AGENT_WALLET_PRIVATE_KEY || '',
    acpApiKey: process.env.ACP_API_KEY || '',
    gameApiKey: process.env.GAME_API_KEY || ''
  };

  if (!config.entityId) {
    throw new Error('AGENT_ENTITY_ID not configured in .env');
  }
  if (!config.privateKey) {
    throw new Error('AGENT_WALLET_PRIVATE_KEY not configured in .env');
  }
  if (!config.acpApiKey) {
    throw new Error('ACP_API_KEY not configured in .env');
  }

  return config;
}

export function createACPClient(): VirtualsACPClient {
  const config = getACPConfig();
  
  return new VirtualsACPClient({
    entityId: config.entityId,
    privateKey: config.privateKey,
    apiKey: config.acpApiKey
  });
}
