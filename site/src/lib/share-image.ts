/**
 * Draws a share card (share-cards.ts) as a 1200×1500 PNG at build time.
 *
 * 4:5 portrait is the largest shape LinkedIn shows in the feed; type sizes are
 * set so names and scores stay readable when it is shrunk to ~360 px wide on a
 * phone. satori lays the card out to SVG, resvg rasterises it; both run only
 * in the build, nothing ships to the browser.
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import satori from 'satori';
import { Resvg } from '@resvg/resvg-js';
import type { Card } from './share-cards';
// Inlined by Vite: a path relative to this file breaks once the build bundles it.
import logoSvg from '../../public/iptc-logo-gradient.svg?raw';

const require = createRequire(import.meta.url);
const fontFile = (subset: string, weight: number) =>
  readFileSync(require.resolve(`@fontsource/nunito-sans/files/nunito-sans-${subset}-${weight}-normal.woff`));

const FONTS = [600, 800, 900].flatMap(weight =>
  ['latin', 'latin-ext', 'cyrillic', 'cyrillic-ext', 'vietnamese'].map(subset => ({
    name: 'Nunito Sans', data: fontFile(subset, weight), weight: weight as 600 | 800 | 900, style: 'normal' as const,
  })),
);

const LOGO = `data:image/svg+xml;base64,${Buffer.from(logoSvg).toString('base64')}`;

const C = {
  navy: '#2f4250', navySoft: '#455f72', green: '#88c16a', greenDark: '#5e9a40',
  muted: '#6b7280', track: '#e8ecef', tint: '#f1f6ee', bg: '#ffffff',
};

type Node = { type: string; props: Record<string, unknown> };
const el = (type: string, style: Record<string, unknown>, children?: unknown, extra: Record<string, unknown> = {}): Node =>
  ({ type, props: { style: { display: 'flex', ...style }, children, ...extra } });

function row(r: Card['rows'][number]): Node {
  return el('div', { alignItems: 'center', gap: 18 }, [
    el('div', { width: 74, fontSize: 44, fontWeight: 900, color: C.navySoft }, r.rank),
    el('div', { flexDirection: 'column', flex: 1, minWidth: 0 }, [
      el('div', { alignItems: 'baseline', gap: 16 }, [
        el('div', { fontSize: 42, fontWeight: 800, color: C.navy, whiteSpace: 'nowrap' }, r.name),
        el('div', { fontSize: 27, fontWeight: 600, color: C.muted, whiteSpace: 'nowrap' }, r.sub),
      ]),
      el('div', { height: 12, marginTop: 6, borderRadius: 6, background: C.track, overflow: 'hidden' }, [
        el('div', { width: `${Math.max(0, Math.min(100, r.score))}%`, height: 12, borderRadius: 6, background: C.green }),
      ]),
    ]),
    el('div', { width: 128, justifyContent: 'flex-end', fontSize: 52, fontWeight: 900, color: C.navy }, r.scoreText ?? String(Math.round(r.score))),
  ]);
}

function words(ctx: Card['context']): Node[] {
  const out: Node[] = [];
  for (const [text, strong] of [[ctx.before, false], [ctx.strong, true], [ctx.after, false]] as const) {
    for (const w of text.split(/\s+/).filter(Boolean)) {
      out.push(el('span', { marginRight: 10, ...(strong ? { color: C.greenDark, fontWeight: 900 } : {}) }, w));
    }
  }
  return out;
}

export async function renderCard(card: Card): Promise<Buffer> {
  const [line1, line2] = card.title.split('\n');
  const tree = el('div', {
    width: 1200, height: 1500, flexDirection: 'column', background: C.bg,
    padding: '64px 72px 52px', fontFamily: 'Nunito Sans', color: C.navy,
  }, [
    el('div', { justifyContent: 'space-between', alignItems: 'flex-start' }, [
      el('div', { flexDirection: 'column', maxWidth: 900 }, [
        el('div', { fontSize: 30, fontWeight: 800, letterSpacing: 2, textTransform: 'uppercase', color: C.greenDark }, card.kicker),
        el('div', { flexDirection: 'column', marginTop: 10, fontSize: 62, fontWeight: 900, lineHeight: 1.05 }, [
          el('div', {}, line1), ...(line2 ? [el('div', {}, line2)] : []),
        ]),
        el('div', { marginTop: 12, fontSize: 28, fontWeight: 600, color: C.muted, lineHeight: 1.3 }, card.subtitle),
      ]),
      el('img', { width: 150, height: 150 }, undefined, { src: LOGO, width: 150, height: 150 }),
    ]),
    // Fixed rhythm rather than space-between: satori doesn't shrink an
    // overfull flex column, so ten rows would run under the context box.
    el('div', { flexDirection: 'column', flex: 1, marginTop: 36, gap: 8 },
      card.rows.map(row)),
    // satori has no inline flow: a span per WORD, wrapping as flex items,
    // reads as one paragraph (a span per phrase wrapped phrase by phrase).
    el('div', { flexWrap: 'wrap', marginTop: 34, padding: '26px 32px', borderRadius: 14, background: C.tint, fontSize: 34, fontWeight: 800, lineHeight: 1.25 },
      words(card.context)),
    el('div', { marginTop: 26, justifyContent: 'space-between', alignItems: 'baseline' }, [
      el('div', { fontSize: 24, fontWeight: 600, color: C.muted }, card.footnote),
      el('div', { fontSize: 34, fontWeight: 900, color: C.navy }, 'metawatch.iptc.org'),
    ]),
  ]);

  const svg = await satori(tree as never, { width: 1200, height: 1500, fonts: FONTS });
  return new Resvg(svg, { fitTo: { mode: 'width', value: 1200 } }).render().asPng();
}
