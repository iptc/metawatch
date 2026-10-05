/** Build-time PNG for each share card: /share/img/<file>.png (see share-cards.ts). */
import type { APIRoute, GetStaticPaths } from 'astro';
import { getShareCards, type ShareCard } from '../../../lib/share-cards';
import { renderCard } from '../../../lib/share-image';

export const getStaticPaths: GetStaticPaths = () =>
  getShareCards().map(share => ({ params: { file: share.file }, props: { share } }));

export const GET: APIRoute = async ({ props }) => {
  const { share } = props as { share: ShareCard };
  return new Response(await renderCard(share.card), { headers: { 'Content-Type': 'image/png' } });
};
