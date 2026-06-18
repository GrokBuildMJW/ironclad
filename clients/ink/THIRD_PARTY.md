# Third-party licenses

This file is **generated** from the resolved lockfile, not hand-maintained. Regenerate with:

```bash
npm run licenses        # fails CI if any non-permissive (GPL/AGPL/LGPL) dep appears
npx license-checker --production --csv > THIRD_PARTY.csv   # full attribution dump
```

Direct dependencies and their licenses (all permissive):

| Package | License |
|---|---|
| ink | MIT |
| react | MIT |
| marked | MIT |
| marked-terminal | MIT |
| typescript | Apache-2.0 |
| tsx | MIT |
| ink-testing-library | MIT |
| react-reconciler | MIT |
| yoga-layout | MIT |
| license-checker | BSD-3-Clause |

Run `npm run licenses` to re-verify against the current lockfile (it fails on any
non-permissive — GPL/AGPL/LGPL — dependency).
