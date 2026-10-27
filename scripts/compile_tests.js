// Compiles FlashArb + the mocks in tests/contracts into build/test_artifacts.json for tests/test_contract.py
const fs = require("fs");
const path = require("path");
const solc = require("solc");

const root = path.join(__dirname, "..");
const input = {
  language: "Solidity",
  sources: {
    "FlashArb.sol": { content: fs.readFileSync(path.join(root, "src", "FlashArb.sol"), "utf8") },
    "Mocks.sol": { content: fs.readFileSync(path.join(root, "tests", "contracts", "Mocks.sol"), "utf8") },
  },
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

const artifacts = {};
for (const [file, contracts] of Object.entries(out.contracts)) {
  for (const [name, c] of Object.entries(contracts)) {
    artifacts[name] = { abi: c.abi, bytecode: "0x" + c.evm.bytecode.object };
  }
}
fs.mkdirSync(path.join(root, "build"), { recursive: true });
fs.writeFileSync(path.join(root, "build", "test_artifacts.json"), JSON.stringify(artifacts));
console.log("compiled:", Object.keys(artifacts).join(", "));
