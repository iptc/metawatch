/**
 * Build-time data loader for the latest crawl run.
 *
 * Reads JSON exported by `crawler/scripts/export_for_site.py`. If the data
 * directory is missing (no crawl has been run yet), returns empty stubs so the
 * site can still build during development.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.resolve(__dirname, '../data/latest');

export interface Summary {
  run_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  site_count_attempted: number;
  site_count_succeeded: number;
  site_count_robots_blocked: number;
  article_count: number;
  image_count: number;
  global_mean_score: number;
  images_with_iptc: number;
  images_with_c2pa: number;
  pct_with_iptc: number;
}

export interface Country {
  country: string;
  site_count: number;
  mean_score: number;
}

export interface Site {
  site_id: string;
  site_name: string;
  country: string;
  category: string;
  status: string;
  discovery_strategy: string;
  sitemap_url_used: string | null;
  articles_sampled: number;
  images_analysed: number;
  mean_iptc_score: number;
  pct_with_iptc: number;
  cdn_breakdown: Record<string, number>;
}

export interface FieldStat {
  field: string;
  present: number;
  total: number;
  pct: number;
}

export interface CdnByProvider {
  provider: string;
  images: number;
  stripped: number;
  pct_stripped: number;
}

export interface CdnData {
  providers: Record<string, number>;
  by_provider: CdnByProvider[];
}

export interface HistoryPoint {
  run_id: string;
  started_at: string;
  site_count: number;
  image_count: number;
  mean_score: number;
}

function readJSON<T>(filename: string, fallback: T): T {
  const p = path.join(DATA_DIR, filename);
  if (!fs.existsSync(p)) return fallback;
  return JSON.parse(fs.readFileSync(p, 'utf-8')) as T;
}

export function getSummary(): Summary {
  return readJSON('summary.json', {
    run_id: null, started_at: null, ended_at: null,
    site_count_attempted: 0, site_count_succeeded: 0, site_count_robots_blocked: 0,
    article_count: 0, image_count: 0, global_mean_score: 0,
    images_with_iptc: 0, images_with_c2pa: 0, pct_with_iptc: 0,
  });
}

export function getCountries(): Country[] {
  return readJSON('countries.json', []);
}

export function getSites(): Site[] {
  return readJSON('sites.json', []);
}

export function getFields(): FieldStat[] {
  return readJSON('fields.json', []);
}

export function getCdn(): CdnData {
  return readJSON('cdn.json', { providers: {}, by_provider: [] });
}

export function getHistory(): HistoryPoint[] {
  return readJSON('history.json', []);
}

export function scoreClass(score: number): string {
  if (score < 25) return 'low';
  if (score < 60) return 'mid';
  return '';
}

const COUNTRY_NAMES: Record<string, string> = {
  AR: 'Argentina', AT: 'Austria', AU: 'Australia', BD: 'Bangladesh', BE: 'Belgium',
  BG: 'Bulgaria', BR: 'Brazil', CA: 'Canada', CH: 'Switzerland', CN: 'China',
  CY: 'Cyprus', CZ: 'Czechia', DE: 'Germany', DK: 'Denmark', EE: 'Estonia',
  EG: 'Egypt', ES: 'Spain', FI: 'Finland', FJ: 'Fiji', FR: 'France',
  GB: 'United Kingdom', GR: 'Greece', HK: 'Hong Kong', HU: 'Hungary', ID: 'Indonesia',
  IE: 'Ireland', IL: 'Israel', IN: 'India', IT: 'Italy', JM: 'Jamaica',
  JP: 'Japan', KR: 'South Korea', LT: 'Lithuania', LV: 'Latvia', MX: 'Mexico',
  NG: 'Nigeria', NL: 'Netherlands', NO: 'Norway', NZ: 'New Zealand', PE: 'Peru',
  PH: 'Philippines', PL: 'Poland', PT: 'Portugal', RO: 'Romania', RU: 'Russia',
  SE: 'Sweden', SG: 'Singapore', SI: 'Slovenia', SK: 'Slovakia', SV: 'El Salvador',
  TH: 'Thailand', TR: 'Türkiye', TW: 'Taiwan', UA: 'Ukraine', US: 'United States',
  VE: 'Venezuela', VN: 'Vietnam', ZA: 'South Africa', ZM: 'Zambia', ZW: 'Zimbabwe',
};

export function countryName(cc: string): string {
  return COUNTRY_NAMES[cc] ?? cc;
}

export function categoryLabel(cat: string): string {
  return cat.replace(/-/g, ' ');
}
