import { createACPClient, getACPConfig } from './acp-client.js';

export class WalletManager {
  private client: any;

  constructor() {
    this.client = createACPClient();
  }

  getAddress(): string {
    const config = getACPConfig();
    return config.walletAddress;
  }

  async getBalance(): Promise<{ usdc: string; virtual: string }> {
    try {
      const balance = await this.client.getWalletBalance();
      return {
        usdc: balance.usdc || '0',
        virtual: balance.virtual || '0'
      };
    } catch (error: any) {
      console.error('[ACP] Failed to fetch balance:', error.message);
      return { usdc: '0', virtual: '0' };
    }
  }

  async getTransactionHistory(limit: number = 10): Promise<any[]> {
    try {
      return await this.client.getTransactions(limit);
    } catch (error: any) {
      console.error('[ACP] Failed to fetch transactions:', error.message);
      return [];
    }
  }
}
