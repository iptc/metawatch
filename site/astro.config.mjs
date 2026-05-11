import { defineConfig } from 'astro/config';

const isProd = process.env.NODE_ENV === 'production';

export default defineConfig({
  site: 'https://iptc.github.io',
  base: isProd ? '/metawatch' : '/',
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
});
