# Third-party licenses

A curated summary of this client's dependencies and their (all permissive) licenses, mirroring
`package.json`. Re-verify / dump full attribution against the resolved lockfile with:

```bash
npm run licenses        # fails if any non-permissive (GPL/AGPL/LGPL) dep appears
npx license-checker --production --csv > THIRD_PARTY.csv   # full attribution dump
```

**Runtime dependencies** (shipped with the installed CLI):

| Package | License |
|---|---|
| marked | MIT |
| marked-terminal | MIT |
| react | MIT |
| react-reconciler | MIT |
| yoga-layout | MIT |

The terminal UI runs on a **purpose-built renderer** (`src/render/`, React reconciler + Yoga
layout) — Stock Ink is **not** a runtime dependency.

**Dev / build / test dependencies** (not shipped):

| Package | License |
|---|---|
| typescript | Apache-2.0 |
| tsx | MIT |
| @types/node | MIT |
| @types/react | MIT |
| @types/react-reconciler | MIT |
| ink | MIT |
| ink-testing-library | MIT |
| license-checker | BSD-3-Clause |

`ink` + `ink-testing-library` are used **only in comparison/spike tests** (benchmarking the custom
renderer against Stock Ink), never in `src/`.
