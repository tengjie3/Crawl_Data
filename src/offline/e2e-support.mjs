import fs from 'node:fs';
import path from 'node:path';

export function browserOptions(env = process.env) {
  return {headless: true, ...(env.CHROME_WRAPPER ? {executablePath: env.CHROME_WRAPPER} : {channel: 'chrome'})};
}

function realDestination(file) {
  try {
    return fs.realpathSync(file);
  } catch (error) {
    const parent = path.dirname(file);
    if (error.code !== 'ENOENT' || parent === file) throw error;
    // The evidence directory may not exist yet; resolve its existing ancestors.
    return path.join(realDestination(parent), path.basename(file));
  }
}

export function packageArguments(argv, script, allowSmoke = false) {
  if (argv.length < 2 || argv.length > (allowSmoke ? 3 : 2)) {
    throw new Error(`Usage: node src/offline/${script} <package-directory> <evidence-directory>${allowSmoke ? ' [full|smoke]' : ''}`);
  }
  const [directory, evidence, mode = 'full'] = argv;
  if (!['full', 'smoke'].includes(mode)) throw new Error('E2E mode must be full or smoke');
  const packageRoot = path.resolve(directory);
  const html = path.join(packageRoot, 'index.html');
  if (!fs.existsSync(html) || !fs.statSync(html).isFile()) throw new Error('Package index.html not found: ' + html);
  const output = path.resolve(evidence);
  const relative = path.relative(fs.realpathSync(packageRoot), realDestination(output));
  if (relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative))) {
    throw new Error('Evidence directory must be outside the package under test');
  }
  return {packageRoot, html, output, mode};
}
