import neo4j, { Driver, Session, SessionMode } from 'neo4j-driver';
import { config } from '../config/env.js';

let driver: Driver | null = null;

export function getDriver(): Driver {
  if (!driver) {
    driver = neo4j.driver(
      config.neo4jUri,
      neo4j.auth.basic(config.neo4jUser, config.neo4jPassword),
      {
        maxConnectionPoolSize: 50,
        connectionAcquisitionTimeout: 30_000,
        logging: {
          level: 'warn',
          logger: (level, message) => console.log(`[neo4j][${level}] ${message}`),
        },
      }
    );
  }
  return driver;
}

export function getSession(mode: SessionMode = neo4j.session.READ): Session {
  return getDriver().session({ defaultAccessMode: mode });
}

export async function closeDriver(): Promise<void> {
  if (driver) {
    await driver.close();
    driver = null;
  }
}

export async function verifyConnectivity(): Promise<void> {
  await getDriver().verifyConnectivity();
}
