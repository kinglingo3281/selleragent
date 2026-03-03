#!/usr/bin/env node

import { Command } from 'commander';

const program = new Command();

program
  .name('acp')
  .description('ACP Agent - ACP Seller CLI')
  .version('1.0.0');

program
  .command('setup')
  .description('Interactive setup for ACP agent')
  .action(async () => {
    console.log('ACP Agent ACP Setup');
    console.log('=====================\n');
    console.log('Please configure your .env file with:');
    console.log('- AGENT_ENTITY_ID (from Virtuals dashboard)');
    console.log('- AGENT_WALLET_PRIVATE_KEY');
    console.log('- ACP_API_KEY');
    console.log('- GAME_API_KEY\n');
    console.log('Then run: npm run sell:create hyperliquid_trade_signal');
  });

program
  .command('sell')
  .description('Manage service offerings')
  .argument('<action>', 'list, create, delete, inspect')
  .argument('[name]', 'offering name')
  .action(async (action: string, name?: string) => {
    const { OfferingManager } = await import('../src/lib/offering-manager.js');
    const manager = new OfferingManager();
    
    try {
      if (action === 'list') {
        const offerings = await manager.listOfferings();
        console.log('ACP Agent Offerings:');
        offerings.forEach(o => console.log(`- ${o}`));
      } else if (action === 'create' && name) {
        await manager.registerOffering(name);
      } else if (action === 'inspect' && name) {
        const details = await manager.getOfferingDetails(name);
        console.log(JSON.stringify(details, null, 2));
      } else {
        console.log('Usage: acp sell <list|create|inspect> [name]');
      }
    } catch (error: any) {
      console.error('Error:', error.message);
      process.exit(1);
    }
  });

program
  .command('resources')
  .description('Run local ACP resource endpoints (localhost only)')
  .argument('<action>', 'start')
  .action(async (action: string) => {
    try {
      if (action === 'start') {
        const { startResourceServer } = await import('../src/lib/resource-server.js');
        await startResourceServer();
      } else {
        console.log('Usage: acp resources <start>');
      }
    } catch (error: any) {
      console.error('Error:', error.message);
      process.exit(1);
    }
  });

let runtime: any = null;

program
  .command('serve')
  .description('Manage seller runtime')
  .argument('<action>', 'start, stop, status')
  .action(async (action: string) => {
    try {
      if (action === 'start') {
        if (runtime && runtime.isRunning()) {
          console.log('[ACP] Seller runtime already running');
          return;
        }
        const { SellerRuntime } = await import('../src/lib/seller-runtime.js');
        runtime = new SellerRuntime();
        await runtime.start();
      } else if (action === 'stop') {
        if (runtime) {
          await runtime.stop();
          runtime = null;
        } else {
          console.log('[ACP] No runtime to stop');
        }
      } else if (action === 'status') {
        if (runtime && runtime.isRunning()) {
          console.log('[ACP] Status: Running');
        } else {
          console.log('[ACP] Status: Stopped');
        }
      } else {
        console.log('Usage: acp serve <start|stop|status>');
      }
    } catch (error: any) {
      console.error('Error:', error.message);
      process.exit(1);
    }
  });

program
  .command('wallet')
  .description('Wallet operations')
  .argument('<action>', 'address, balance')
  .action(async (action: string) => {
    const { WalletManager } = await import('../src/lib/wallet-manager.js');
    const wallet = new WalletManager();
    
    try {
      if (action === 'address') {
        console.log('ACP Agent Wallet:', wallet.getAddress());
      } else if (action === 'balance') {
        const balance = await wallet.getBalance();
        console.log('Wallet Balance:');
        console.log(`  USDC: ${balance.usdc}`);
        console.log(`  VIRTUAL: ${balance.virtual}`);
      } else {
        console.log('Usage: acp wallet <address|balance>');
      }
    } catch (error: any) {
      console.error('Error:', error.message);
      process.exit(1);
    }
  });

program
  .command('whoami')
  .description('Show ACP agent info')
  .action(async () => {
    try {
      const { getACPConfig } = await import('../src/lib/acp-client.js');
      const config = getACPConfig();
      console.log('ACP Agent Info:');
      console.log(`  Entity ID: ${config.entityId}`);
      console.log(`  Wallet: ${config.walletAddress}`);
    } catch (error: any) {
      console.error('Error:', error.message);
      process.exit(1);
    }
  });

program.parse();
