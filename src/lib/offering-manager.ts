import { createACPClient } from './acp-client.js';
import { readFile } from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export interface Offering {
  name: string;
  displayName: string;
  description: string;
  price: string;
  currency: string;
  requirements: any;
  deliverables: any;
}

export class OfferingManager {
  private client: any;

  constructor() {
    this.client = createACPClient();
  }

  async loadOffering(offeringName: string): Promise<Offering> {
    const offeringPath = path.join(
      __dirname,
      '../offerings',
      offeringName,
      'offering.json'
    );

    const content = await readFile(offeringPath, 'utf-8');
    return JSON.parse(content);
  }

  async registerOffering(offeringName: string): Promise<void> {
    console.log(`[ACP] Registering offering: ${offeringName}`);
    
    const offering = await this.loadOffering(offeringName);
    
    try {
      await this.client.registerOffering({
        name: offering.name,
        displayName: offering.displayName,
        description: offering.description,
        price: offering.price,
        currency: offering.currency,
        requirements: offering.requirements,
        deliverables: offering.deliverables
      });
      
      console.log(`[ACP] Successfully registered: ${offeringName}`);
    } catch (error: any) {
      console.error(`[ACP] Failed to register ${offeringName}:`, error.message);
      throw error;
    }
  }

  async listOfferings(): Promise<string[]> {
    const offeringsDir = path.join(__dirname, '../offerings');
    const { readdir } = await import('fs/promises');
    
    const dirs = await readdir(offeringsDir, { withFileTypes: true });
    return dirs.filter(d => d.isDirectory()).map(d => d.name);
  }

  async getOfferingDetails(offeringName: string): Promise<Offering> {
    return this.loadOffering(offeringName);
  }
}
