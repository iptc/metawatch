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
import c2paJson from '../data/latest/c2pa.json';
import dstJson from '../data/latest/dst.json';
import scoringJson from '../data/latest/scoring.json';
import historyJson from '../data/latest/history.json';
import historyBySiteJson from '../data/latest/history_by_site.json';
import historyByCountryJson from '../data/latest/history_by_country.json';
import runsIndexJson from '../data/latest/runs_index.json';
import countryNamesJson from '../data/latest/countries_names.json';
import fieldsBySiteJson from '../data/latest/fields_by_site.json';
import historyFieldsBySiteJson from '../data/latest/history_fields_by_site.json';
import c2paBySiteJson from '../data/latest/c2pa_by_site.json';
import dstBySiteJson from '../data/latest/dst_by_site.json';
import samplesBySiteJson from '../data/latest/samples_by_site.json';
import aiPolicyJson from '../data/latest/ai_policy.json';

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
  pct_with_c2pa: number;
}

export type C2paOutcome = 'valid' | 'modified' | 'expired' | 'untrusted_issuer' | 'other_invalid';

export interface DstUriRow {
  uri: string;
  term: string | null;
  bucket: string;
  images: number;
}

export interface DstBucketRow {
  bucket: string;
  images: number;
}

export interface DstData {
  image_count_total: number;
  image_count_valid: number;
  images_with_dst_iptc: number;
  images_with_dst_c2pa: number;
  pct_with_dst_iptc: number;
  by_bucket_iptc: DstBucketRow[];
  by_bucket_c2pa: DstBucketRow[];
  by_uri_iptc: DstUriRow[];
  by_uri_c2pa: DstUriRow[];
}

export interface C2paData {
  image_count_total: number;
  image_count_with_c2pa: number;
  pct_with_c2pa: number;
  by_outcome: { outcome: C2paOutcome; images: number }[];
  by_signer: { signer: string; images: number }[];
  by_validation_state: { state: string; images: number }[];
  top_sites: { site_id: string; site_name: string; country: string; images_with_c2pa: number }[];
}

export interface Country {
  country: string;
  site_count: number;
  mean_score: number;
}

export interface Site {
  site_id: string;
  site_name: string;
  url: string;
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

export interface SiteFieldStat {
  field: string;
  present: number;
  total: number;
  pct: number;
  scored: boolean;
}

export interface FieldStat {
  field: string;
  present: number;
  total: number;
  pct: number;
  weight: number;
  weight_pct: number;
  scored: boolean;
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
  images_with_iptc: number;
  pct_with_iptc: number;
  images_with_c2pa: number;
  pct_with_c2pa: number;
  c2pa_outcomes: Partial<Record<C2paOutcome, number>>;
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

export function getC2pa(): C2paData {
  return c2paJson as C2paData;
}

export function getDst(): DstData {
  return dstJson as DstData;
}

export interface AiPolicySignal {
  key: string;
  scope: 'site' | 'image';
  label: string;
  note: string;
  num: number;
  denom: number;
  pct: number;
}

export interface AiPolicyBot {
  ua: string;
  operator: string;
  blocked: number;
  total: number;
  pct_blocked: number;
}

export interface AiPolicyBucket {
  range: string;
  lo: number;
  hi: number;
  count: number;
  pct: number;
}

export interface AiPolicyConvergence {
  a: string;
  b: string;
  a_count: number;
  both: number;
  pct: number;
}

export interface AiPolicyData {
  n_sites: number;
  n_images: number;
  signals: AiPolicySignal[];
  ai_bots: AiPolicyBot[];
  block_buckets: AiPolicyBucket[];
  convergence: AiPolicyConvergence[];
}

export function getAiPolicy(): AiPolicyData {
  return aiPolicyJson as AiPolicyData;
}

export interface ScoredField {
  label: string;
  aliases: string[];
  weight: number;
}
export interface TrackedField {
  label: string;
  aliases: string[];
}
export interface ScoringConfig {
  scored_fields: ScoredField[];
  tracked_fields: TrackedField[];
  total_weight: number;
}

export function getScoring(): ScoringConfig {
  return scoringJson as ScoringConfig;
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

export function getSiteFieldStats(siteId: string): SiteFieldStat[] {
  return ((fieldsBySiteJson as Record<string, SiteFieldStat[]>)[siteId]) ?? [];
}

export function getSiteFieldHistory(siteId: string): Record<string, SeriesPoint[]> {
  return ((historyFieldsBySiteJson as Record<string, Record<string, SeriesPoint[]>>)[siteId]) ?? {};
}

export interface SiteC2paData {
  image_count_with_c2pa: number;
  by_outcome: { outcome: C2paOutcome; images: number }[];
  by_signer: { signer: string; images: number }[];
  by_validation_state: { state: string; images: number }[];
}

export function getSiteC2pa(siteId: string): SiteC2paData | null {
  return (c2paBySiteJson as Record<string, SiteC2paData>)[siteId] ?? null;
}

export interface SiteDstData {
  images_with_dst_iptc: number;
  images_with_dst_c2pa: number;
  by_bucket_iptc: DstBucketRow[];
  by_bucket_c2pa: DstBucketRow[];
  by_uri_iptc: DstUriRow[];
  by_uri_c2pa: DstUriRow[];
}

export function getSiteDst(siteId: string): SiteDstData | null {
  return (dstBySiteJson as Record<string, SiteDstData>)[siteId] ?? null;
}

export interface SampleRow {
  article_url: string;
  title: string | null;
  publication_date: string | null;
  article_http_status: number | null;
  image_url?: string;
  image_http_status?: number | null;
  iptc_score?: number;
  has_exif?: boolean;
  has_iptc_iim?: boolean;
  has_iptc_xmp?: boolean;
  has_c2pa?: boolean;
  c2pa_signer?: string | null;
  c2pa_validation_status?: string | null;
  cdn_provider?: string;
  cdn_optimizer_active?: string | null;
  mime_type?: string | null;
  width?: number | null;
  height?: number | null;
  file_size_bytes?: number | null;
  dst_iptc?: string | null;
  dst_c2pa?: string[];
  /** Field labels (from scoring.yaml) that ARE present on this image.
   * Anything absent from this list is missing — the page diffs against the
   * full ScoringConfig.scored_fields + tracked_fields lists. */
  present_fields?: string[];
  /** Raw exiftool key/value pairs from the IPTC + XMP groups for this image.
   * Values may be strings, numbers, or lists; the page stringifies them for
   * display. {} when the image carried nothing. */
  metadata?: Record<string, unknown>;
}

export function getSiteSamples(siteId: string): SampleRow[] {
  return ((samplesBySiteJson as Record<string, SampleRow[]>)[siteId]) ?? [];
}

export function scoreClass(score: number): string {
  if (score < 25) return 'low';
  if (score < 60) return 'mid';
  return '';
}

// Country-code → display name. Comes from config/countries.yaml via
// export_for_site.py; editing the YAML and re-exporting is the supported
// way to add a label. The fallback to `cc` is for any country that has
// publishers but isn't (yet) in the config — the UI will at least show
// the code rather than crashing.
const COUNTRY_NAMES = countryNamesJson as Record<string, string>;

export function countryName(cc: string): string {
  return COUNTRY_NAMES[cc] ?? cc;
}

export function categoryLabel(cat: string): string {
  return cat.replace(/-/g, ' ');
}
