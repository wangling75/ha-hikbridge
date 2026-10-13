import { createRequire } from "node:module";

const command = process.argv[2];
if (!command) {
    console.error("usage: node scripts/scrypted-cli.mjs <scrypted-command> [...args]");
    process.exit(1);
}

process.argv = [process.argv[0], command, ...process.argv.slice(3)];
const require = createRequire(import.meta.url);
require("../node_modules/@scrypted/sdk/bin/cli.js");
