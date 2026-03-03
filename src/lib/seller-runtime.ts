import { createACPClient, getACPConfig } from './acp-client.js';
import { readdir } from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

interface OfferingHandler {
  executeJob: (job: any) => Promise<any>;
  validateJob?: (job: any) => Promise<boolean>;
}

export class SellerRuntime {
  private client: any;
  private offerings: Map<string, OfferingHandler> = new Map();
  private running: boolean = false;

  constructor() {
    this.client = createACPClient();
  }

  async loadOfferings() {
    const offeringsDir = path.join(__dirname, '../offerings');
    
    try {
      const dirs = await readdir(offeringsDir, { withFileTypes: true });
      
      for (const dir of dirs) {
        if (dir.isDirectory()) {
          const offeringName = dir.name;
          const handlerPath = path.join(offeringsDir, offeringName, 'handlers.js');
          
          try {
            const handler = await import(handlerPath);
            this.offerings.set(offeringName, handler);
            console.log(`[ACP] Loaded offering: ${offeringName}`);
          } catch (error) {
            console.error(`[ACP] Failed to load handler for ${offeringName}:`, error);
          }
        }
      }
      
      console.log(`[ACP] Loaded ${this.offerings.size} offering(s)`);
    } catch (error) {
      console.error('[ACP] Failed to load offerings:', error);
    }
  }

  async start() {
    if (this.running) {
      console.log('[ACP] Seller runtime already running');
      return;
    }

    const config = getACPConfig();
    console.log('[ACP] Starting seller runtime...');
    console.log(`[ACP] Entity ID: ${config.entityId}`);
    console.log(`[ACP] Wallet: ${config.walletAddress}`);
    
    await this.loadOfferings();
    
    this.running = true;
    console.log('[ACP] Seller runtime started');
    console.log('[ACP] Listening for jobs from ACP network...');
    
    await this.listenForJobs();
  }

  async listenForJobs() {
    while (this.running) {
      try {
        const jobs = await this.client.getActiveJobs();
        
        for (const job of jobs) {
          if (job.status === 'pending' && !job.processed) {
            await this.handleJob(job);
          }
        }
        
        await new Promise(resolve => setTimeout(resolve, 5000));
      } catch (error) {
        console.error('[ACP] Error polling jobs:', error);
        await new Promise(resolve => setTimeout(resolve, 10000));
      }
    }
  }

  async handleJob(job: any) {
    const offeringName = job.offeringName;
    const handler = this.offerings.get(offeringName);
    
    if (!handler) {
      console.error(`[ACP] No handler for offering: ${offeringName}`);
      return;
    }

    console.log(`[ACP] Processing job ${job.id} for ${offeringName}`);
    
    try {
      if (handler.validateJob) {
        const valid = await handler.validateJob(job);
        if (!valid) {
          console.error(`[ACP] Job validation failed: ${job.id}`);
          return;
        }
      }

      const deliverable = await handler.executeJob(job);
      
      await this.client.submitDeliverable(job.id, deliverable);
      
      console.log(`[ACP] Job ${job.id} completed successfully`);
    } catch (error: any) {
      console.error(`[ACP] Job ${job.id} failed:`, error.message);
      
      await this.client.failJob(job.id, error.message);
    }
  }

  async stop() {
    console.log('[ACP] Stopping seller runtime...');
    this.running = false;
    console.log('[ACP] Seller runtime stopped');
  }

  isRunning(): boolean {
    return this.running;
  }
}
