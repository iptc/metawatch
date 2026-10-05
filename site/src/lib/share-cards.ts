/**
 * The monthly share cards: what each image says, drawn from the latest run.
 *
 * One definition feeds both the PNG endpoint (share-image.ts draws it) and the
 * /share/ pages (title, description, og:image), so an image and the page that
 * shares it can't disagree.
 *
 * File names carry the run month (top-publishers-2026-10.png): LinkedIn and
 * friends cache a link preview by image URL, so a stable name would keep
 * showing last month's numbers after the site rebuilds.
 */
import {
  getSites, getCountries, getSummary, getLatestCountryRanks, countryName,
  MIN_RANKED_IMAGES, MIN_RANKED_SITES, type Site,
} from './data';

export interface CardRow {
  rank: string; name: string; sub: string;
  score: number;          // 0–100, drives the bar
  scoreText?: string;     // as printed; defaults to the whole number
}
export interface Card {
  kicker: string;
  title: string;          // "\n" splits it onto two lines
  subtitle: string;
  rows: CardRow[];
  context: { before: string; strong: string; after: string };
  footnote: string;
}
export interface ShareCard {
  slug: string;           // page: /share/<slug>/
  file: string;           // image: /share/img/<file>.png
  title: string;          // page title and share text
  description: string;
  card: Card;
}

const summary = getSummary();
const runDate = summary.started_at ? new Date(summary.started_at) : new Date();
const monthLabel = runDate.toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' });
const monthSlug = runDate.toISOString().slice(0, 7);
const kicker = `IPTC Metawatch · ${monthLabel}`;

/** Equal scores share a rank: 1, 2, 2=, … → "8=", "8=", "10". */
function rankLabels(scores: number[]): string[] {
  return scores.map(s => {
    const first = scores.indexOf(s);
    const tied = scores.filter(t => t === s).length > 1;
    return `${first + 1}${tied ? '=' : ''}`;
  });
}

/**
 * The card font covers Latin, Cyrillic and Vietnamese, not CJK. Every CJK
 * publisher name we carry also has a Latin form in brackets or before them
 * ("Asahi Shimbun (朝日新聞)", "即時/娛樂 (United Daily News)"): keep that.
 */
const NOT_IN_FONT = /[⺀-鿿가-힯豈-﫿＀-￯]/;
// Our own disambiguators for a publisher's language editions, e.g.
// "Spiegel Online (German)": useful in a table, noise on a card.
const EDITION = /\s*\((English|German|French|Spanish|Russian|Arabic|Portuguese|Ukrainian|Chinese|Japanese|Korean|Italian|Turkish)\)$/;

export function cardName(raw: string): string {
  const name = raw.replace(EDITION, '');
  if (!NOT_IN_FONT.test(name)) return name;
  const m = name.match(/^(.*?)\s*\(([^)]*)\)\s*$/);
  if (m) {
    const [, outer, inner] = m;
    if (!NOT_IN_FONT.test(outer)) return outer.trim();
    if (!NOT_IN_FONT.test(inner)) return inner.trim();
  }
  return name.replace(new RegExp(NOT_IN_FONT.source, 'g'), '').replace(/\(\s*\)/g, '').trim();
}

const scored = getSites().filter(s => s.status === 'ok' && s.images_analysed > 0);
const zeroCount = scored.filter(s => s.mean_iptc_score === 0).length;
const SCORE_LINE = "Score out of 100: how much of the key photo metadata survives on each publisher's lead photos";

function publisherRows(sites: Site[], withCountry: boolean, minImages = MIN_RANKED_IMAGES): CardRow[] {
  const top = sites
    .filter(s => s.images_analysed >= minImages)
    .sort((a, b) => b.mean_iptc_score - a.mean_iptc_score)
    .slice(0, 10);
  const ranks = rankLabels(top.map(s => s.mean_iptc_score));
  return top.map((s, i) => ({
    rank: ranks[i],
    name: cardName(s.site_name),
    sub: withCountry ? countryName(s.country) : `${s.images_analysed} photos`,
    score: s.mean_iptc_score,
  }));
}

function topPublishers(): ShareCard {
  return {
    slug: 'top-publishers',
    file: `top-publishers-${monthSlug}`,
    title: `Top 10 news publishers for photo metadata, ${monthLabel}`,
    description: `Which news publishers keep photographers' credits, captions and rights in their photos. IPTC Metawatch, ${monthLabel}.`,
    card: {
      kicker,
      title: 'Top 10 news publishers\nfor photo metadata',
      subtitle: SCORE_LINE,
      rows: publisherRows(scored, true),
      context: { before: 'For comparison: ', strong: `${zeroCount} of ${scored.length}`, after: ' publishers scored zero.' },
      footnote: `Min. ${MIN_RANKED_IMAGES} photos sampled · ${summary.site_count_attempted} publishers in ${getCountries().length} countries`,
    },
  };
}

const { ranks: countryRanks } = getLatestCountryRanks();
const rankedCountries = getCountries()
  .filter(c => countryRanks.has(c.country))
  .sort((a, b) => countryRanks.get(a.country)!.rank - countryRanks.get(b.country)!.rank);

function topCountries(): ShareCard {
  const top = rankedCountries.slice(0, 10);
  return {
    slug: 'top-countries',
    file: `top-countries-${monthSlug}`,
    title: `Top 10 countries for news photo metadata, ${monthLabel}`,
    description: `Countries ranked by how much photo metadata their news publishers keep. IPTC Metawatch, ${monthLabel}.`,
    card: {
      kicker,
      title: 'Top 10 countries\nfor news photo metadata',
      subtitle: "Average score of each country's news publishers, out of 100",
      rows: top.map(c => {
        const r = countryRanks.get(c.country)!;
        const tied = rankedCountries.filter(o => countryRanks.get(o.country)!.rank === r.rank).length > 1;
        // One decimal: country means sit close together (19.2 vs 18.8), and
        // whole numbers would print two different ranks as the same score.
        return { rank: `${r.rank}${tied ? '=' : ''}`, name: countryName(c.country), sub: `${c.scored_site_count} publishers`, score: c.mean_score, scoreText: c.mean_score.toFixed(1) };
      }),
      context: { before: 'Ranked: ', strong: `${rankedCountries.length} countries`, after: ` with more than ${MIN_RANKED_SITES} publishers scored.` },
      footnote: `${summary.site_count_attempted} publishers in ${getCountries().length} countries`,
    },
  };
}

/**
 * A card per ranked country (the same bar as the country rankings), listing
 * its own publishers. Skipped when fewer than three of them have enough
 * photos to rank — a one-line card says nothing.
 */
function countryCards(): ShareCard[] {
  const out: ShareCard[] = [];
  for (const c of rankedCountries) {
    const sites = scored.filter(s => s.country === c.country);
    // Every scored publisher, not just those with 10+ photos: the country's
    // rank and average are built from all of them, and hiding one (Taiwan's
    // ETtoday, on 2 photos, carries its whole average) made the card
    // contradict its own headline. Each row prints its photo count instead.
    const rows = publisherRows(sites, false, 1);
    if (rows.length < 3) continue;
    const name = countryName(c.country);
    const r = countryRanks.get(c.country)!;
    const zero = sites.filter(s => s.mean_iptc_score === 0).length;
    out.push({
      slug: `country-${c.country.toLowerCase()}`,
      file: `country-${c.country.toLowerCase()}-${monthSlug}`,
      title: `How ${name}'s news publishers score for photo metadata, ${monthLabel}`,
      description: `${name} ranks #${r.rank} of ${rankedCountries.length} countries for keeping photo metadata. IPTC Metawatch, ${monthLabel}.`,
      card: {
        kicker,
        title: `${name}: news publishers\nand photo metadata`,
        subtitle: SCORE_LINE,
        rows,
        context: {
          before: `${name} ranks `,
          strong: `#${r.rank} of ${rankedCountries.length}`,
          after: ` countries, average ${c.mean_score.toFixed(1)}. ${zero} of ${sites.length} publishers scored zero.`,
        },
        footnote: `Countries ranked with more than ${MIN_RANKED_SITES} publishers scored`,
      },
    });
  }
  return out;
}

export function getShareCards(): ShareCard[] {
  return [topPublishers(), topCountries(), ...countryCards()];
}

export function getCountryShareCard(cc: string): ShareCard | undefined {
  return getShareCards().find(s => s.slug === `country-${cc.toLowerCase()}`);
}

export function shareImagePath(s: ShareCard): string {
  return `/share/img/${s.file}.png`;
}

/** The card's content as text, for alt attributes and screen readers. */
export function cardAlt(s: ShareCard): string {
  const rows = s.card.rows.map(r => `${r.rank} ${r.name} (${r.sub}) ${r.scoreText ?? Math.round(r.score)}`).join('; ');
  const ctx = `${s.card.context.before}${s.card.context.strong}${s.card.context.after}`;
  return `${s.title}. ${rows}. ${ctx}`;
}
