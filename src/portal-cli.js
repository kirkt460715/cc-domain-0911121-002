import {promises as fs} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Harbor} from './service.js';
import {HarborError, demand, decode} from './primitives.js';
import {
  bootstrapPortal, receivePortalExport, tracePortalExport, provenanceContract,
} from './portalbridge.js';

function usage() {
  return {
    usage: 'node src/portal-cli.js COMMAND --root DIRECTORY --actor STEWARD --input JSON_FILE [--csv FILE]',
    commands: ['portal-contract', 'portal-bootstrap', 'portal-receive', 'portal-trace'],
    note: 'portal-receive reads CSV bytes from --csv (untrusted download kept in its own directory)',
  };
}

export async function runPortalCli(argv) {
  const [command = 'help', ...rest] = argv;
  const options = {};
  for (let position = 0; position < rest.length; position += 2) {
    const option = rest[position];
    const value = rest[position + 1];
    demand(option.startsWith('--') && value !== undefined, 'arguments', 'Options require --name value pairs');
    demand(!Object.hasOwn(options, option.slice(2)), 'arguments', 'Repeated command option');
    options[option.slice(2)] = value;
  }
  if (command === 'help' || command === 'portal-contract') {
    return command === 'help' ? usage() : provenanceContract();
  }
  demand(options.root, 'arguments', '--root is required');
  const harbor = new Harbor(options.root);
  let input = {};
  if (options.input) {
    const stat = await fs.stat(options.input);
    demand(stat.isFile() && stat.size <= 32 * 1024 * 1024, 'input_size', 'Input must be a bounded regular file');
    input = decode(await fs.readFile(options.input, 'utf8'));
  }
  switch (command) {
    case 'portal-bootstrap':
      return bootstrapPortal(harbor);
    case 'portal-receive': {
      demand(options.csv, 'arguments', '--csv is required for portal-receive');
      const csvStat = await fs.lstat(options.csv);
      demand(csvStat.isFile() && !csvStat.isSymbolicLink(), 'unsafe_path', 'CSV must be a regular file');
      demand(csvStat.size <= 32 * 1024 * 1024, 'input_size', 'CSV exceeds the bounded size');
      // 下载文件是不可信输入：仅按路径读取字节，工作目录始终在文件目录之外。
      const csv = await fs.readFile(options.csv, 'utf8');
      return receivePortalExport(harbor, {...input, csv}, options.actor);
    }
    case 'portal-trace':
      return harbor.read((index, _at) => tracePortalExport(harbor.vault, index, input));
    default:
      demand(false, 'command', 'Unknown portal command');
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const output = await runPortalCli(process.argv.slice(2));
    process.stdout.write(JSON.stringify(output, null, 2) + String.fromCharCode(10));
  } catch (error) {
    const document = error instanceof HarborError ? error.document() : {error: 'internal', message: error.message};
    process.stderr.write(JSON.stringify(document) + String.fromCharCode(10));
    process.exitCode = 1;
  }
}
