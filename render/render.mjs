// Render por lotes: un solo bundle, N módulos.
// Uso: node render.mjs <jobs.json>
// jobs.json: {"jobs":[{"id":1,"out":"/abs/path/H1.mp4","props":{...}}]}
// Emite una línea JSON por trabajo: {"done":id} | {"failed":id,"error":"..."}
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition} from '@remotion/renderer';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const jobsFile = process.argv[2];
if (!jobsFile) {
  console.error('uso: node render.mjs <jobs.json>');
  process.exit(1);
}
const {jobs} = JSON.parse(fs.readFileSync(jobsFile, 'utf8'));

const serveUrl = await bundle({
  entryPoint: path.join(here, 'src', 'index.ts'),
  publicDir: path.join(here, 'public'),
});

let failed = 0;
for (const job of jobs) {
  try {
    const composition = await selectComposition({
      serveUrl,
      id: 'Module',
      inputProps: job.props,
    });
    await renderMedia({
      serveUrl,
      composition,
      codec: 'h264',
      audioCodec: 'aac',
      crf: 18,
      inputProps: job.props,
      outputLocation: job.out,
    });
    console.log(JSON.stringify({done: job.id, out: job.out}));
  } catch (err) {
    failed++;
    console.log(JSON.stringify({failed: job.id, error: String(err).slice(0, 300)}));
    console.error(`[render] job ${job.id}: ${err}`);
  }
}
// Falla el proceso solo si no se pudo renderizar nada
process.exit(failed === jobs.length && jobs.length > 0 ? 1 : 0);
