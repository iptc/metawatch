/**
 * Build-time data loader for the latest crawl run.
 *
 * The JSON files under ../data/latest/ are exported by
 * `crawler/scripts/export_for_site.py` and imported directly here so Vite
 * inlines their contents at build time. This is more reliable than reading
 * from disk via __dirname/path.resolve, which broke under production builds
 * because the bundled module runs from a different cwd than the source tree.
 */

import summaryJson from '../data/latest/summary.json';
import countriesJson from '../data/latest/countries.json';
import sitesJson from '../data/latest/sites.json';
import fieldsJson from '../data/latest/fields.json';
import cdnJson from '../data/latest/cdn.json';
import historyJson from '../data/latest/history.json';
import historyBySiteJson from '../data/latest/history_by_site.json';
import historyByCountryJson from '../data/latest/history_by_country.json';
import runsIndexJson from '../data/latest/runs_index.json';

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
  weight: number;
  weight_pct: number;
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

export interface RunFile {
  name: string;
  size_bytes: number;
}

export interface RunIndexEntry {
  run_id: string;
  started_at: string | null;
  ended_at: string | null;
  site_count_attempted: number;
  site_count_succeeded: number;
  image_count: number;
  directory: string;
  files: RunFile[];
}

export function getSummary(): Summary {
  return summaryJson as Summary;
}

export function getCountries(): Country[] {
  return countriesJson as Country[];
}

export function getSites(): Site[] {
  return sitesJson as Site[];
}

export function getFields(): FieldStat[] {
  return fieldsJson as FieldStat[];
}

export function getCdn(): CdnData {
  return cdnJson as CdnData;
}

export function getHistory(): HistoryPoint[] {
  return historyJson as HistoryPoint[];
}

export function getRunsIndex(): RunIndexEntry[] {
  return runsIndexJson as RunIndexEntry[];
}

export interface SeriesPoint { x: string; y: number; }

export function getSiteHistory(siteId: string): SeriesPoint[] {
  return ((historyBySiteJson as Record<string, SeriesPoint[]>)[siteId]) ?? [];
}

export function getCountryHistory(cc: string): SeriesPoint[] {
  return ((historyByCountryJson as Record<string, SeriesPoint[]>)[cc]) ?? [];
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
  GB: 'United Kingdom', GR: 'Greece', HK: 'Hong Kong', HR: 'Croatia',
  HU: 'Hungary', ID: 'Indonesia', IE: 'Ireland', IL: 'Israel', IN: 'India',
  IT: 'Italy', JM: 'Jamaica', JP: 'Japan', KE: 'Kenya',
  KO: 'South Korea', KR: 'South Korea', LT: 'Lithuania', LU: 'Luxembourg',
  LV: 'Latvia', MT: 'Malta', MX: 'Mexico', MY: 'Malaysia',
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
