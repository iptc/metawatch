import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://metawatch.iptc.org',
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
});
