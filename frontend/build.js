#!/usr/bin/env node
// r42 fix #1.27: pre-bundle app.js with esbuild so production stops shipping
// Babel-standalone (>1MB runtime) and the JSX-in-browser eval path. Output
// is a single self-contained app.compiled.js that index.html loads directly.
//
// Usage:  node build.js [--watch]
//
// We deliberately do NOT bundle React / lightweight-charts here — those
// load from CDN with SRI hashes (see index.html), and bundling them would
// invalidate the SRI mechanism. esbuild marks them as `globalName` shims.

const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');

const watch = process.argv.includes('--watch');

const OUT = path.join(__dirname, 'app.compiled.js');

const opts = {
  entryPoints: [path.join(__dirname, 'app.js')],
  outfile: OUT,
  bundle: false,            // single-file source, no imports
  loader: { '.js': 'jsx' },
  jsx: 'transform',         // classic React.createElement pragma
  jsxFactory: 'React.createElement',
  jsxFragment: 'React.Fragment',
  target: 'es2020',
  minify: true,
  sourcemap: true,
  legalComments: 'none',
  logLevel: 'info',
};

async function run() {
  if (watch) {
    const ctx = await esbuild.context(opts);
    await ctx.watch();
    console.log('[build] watching app.js for changes...');
  } else {
    const t0 = Date.now();
    await esbuild.build(opts);
    const ms = Date.now() - t0;
    const size = fs.statSync(OUT).size;
    console.log(`[build] wrote ${path.relative(process.cwd(), OUT)} (${(size/1024).toFixed(1)} KB) in ${ms} ms`);
  }
}

run().catch(e => { console.error(e); process.exit(1); });
