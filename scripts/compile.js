// Compiles src/FlashArb.sol with solc-js and writes build/FlashArb.json ({abi, bytecode}).
// evmVersion is pinned to "paris" so the bytecode has no PUSH0 and runs on every chain.
const fs = require("fs");
const path = require("path");
const solc = require("solc");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "src", "FlashArb.sol"), "utf8");

const input = {
  language: "Solidity",
  sources: { "FlashArb.sol": { content: source } },
  settings: {
    optimizer: { enabled: true, runs: 200 },
    evmVersion: "paris",
    outputSelection: { "*": { "*": ["abi", "evm.bytecode.object"] } },
  },
};

const out = JSON.parse(solc.compile(JSON.stringify(input)));
const issues = out.errors || [];
issues.forEach((e) => console.log(e.formattedMessage));
if (issues.some((e) => e.severity === "error")) process.exit(1);

const c = out.contracts["FlashArb.sol"]["FlashArb"];
fs.mkdirSync(path.join(root, "build"), { recursive: true });
fs.writeFileSync(
  path.join(root, "build", "FlashArb.json"),
  JSON.stringify({ abi: c.abi, bytecode: "0x" + c.evm.bytecode.object }, null, 2)
);
console.log("compiled FlashArb -> build/FlashArb.json");
