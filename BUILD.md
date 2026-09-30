# Building the uP Trend Chart

## Requirements

- Python 3.11 or newer
- Bash
- Node.js only for the interactive site

From the repository root, run:

```sh
./generate_uptrend.sh
```

The script creates `.venv/`, installs `requirements.txt`, rebuilds the
historical processor input CSV, and generates `uptrend/uptrend-chart.png`,
`.svg`, `.pdf`, and `.eps`. It also regenerates the device and observation CSVs
and `uptrend/uptrend-chart-data.json`. The local environment is ignored by Git.
All four chart exports use the same 2300×1500 layout and labels.

To regenerate selected formats after setup:

```sh
.venv/bin/python generate_uptrend.py --formats svg pdf
```

Run `generate_uptrend_legacy.py` first if you change data under
`uptrend/legacy-inputs/`.

## Interactive site

```sh
cd uptrend/site
npm install
npm run dev
```

Run `npm run build` for a static build in `uptrend/site/dist/`. Regenerate the
chart before building the site after changing chart data. The optional
`npm run preview` and `npm run deploy` commands use a local, Git-ignored
`wrangler.jsonc` whose `assets.directory` is `./dist`.
