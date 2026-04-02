import dotenv from 'dotenv';

dotenv.config();

export interface AppConfig {
  neo4jUri: string;
  neo4jUser: string;
  neo4jPassword: string;
  port: number;
  logLevel: string;
}

function requireEnv(key: string): string {
  const value = process.env[key];
  if (!value) {
    throw new Error(`Required environment variable ${key} is not set`);
  }
  return value;
}

function optionalEnv(key: string, fallback: string): string {
  return process.env[key] || fallback;
}

export const config: AppConfig = {
  neo4jUri: optionalEnv('NEO4J_URI', 'bolt://localhost:7687'),
  neo4jUser: optionalEnv('NEO4J_USER', 'neo4j'),
  neo4jPassword: requireEnv('NEO4J_PASSWORD'),
  port: parseInt(optionalEnv('PORT', '3000'), 10),
  logLevel: optionalEnv('LOG_LEVEL', 'info'),
};
