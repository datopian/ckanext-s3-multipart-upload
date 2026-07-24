import * as esbuild from 'esbuild'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const outfile = path.join(
  __dirname,
  '..',
  'ckanext',
  's3_multipart_upload',
  'assets',
  'js',
  'upload.js'
)

await esbuild.build({
  entryPoints: [path.join(__dirname, 'src', 'upload.js')],
  bundle: true,
  minify: true,
  sourcemap: false,
  format: 'iife',
  target: ['es2018'],
  outfile
})

console.log('Built', outfile)
