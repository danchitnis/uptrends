# Building the Charts

## Requirements

- Python 3.10 or newer
- gnuplot with Cairo terminal support
- Python packages listed in `requirements.txt`

Install the Python packages:

```sh
python3 -m pip install -r requirements.txt
```

## Generate all charts

```sh
python3 generate_plots.py
```

## Select editions

```sh
python3 generate_plots.py --editions 40 48
```

Available editions are `40`, `42`, `48`, and `50`.

## Select formats

```sh
python3 generate_plots.py --formats png svg pdf eps
```

Available formats are `png`, `svg`, `pdf`, and `eps`.

Edition and format selections can be combined:

```sh
python3 generate_plots.py --editions 42 48 --formats svg pdf
```

Generated files are written to the corresponding year directory.

## Generate the 50-year CSV exports

```sh
python3 generate_50yrs_csv.py
```

The complete ledger and processor view are written to `50yrs/`; the verified-only
point export is written to `50yrs-verified/` after checking the dataset.

## Generate the verified-only 50-year chart

```sh
python3 generate_plots.py --verified-only
```

The PNG, SVG, PDF, and EPS outputs are written to `50yrs-verified/`.

## Generate the CPU/GPU compute-trends edition

```sh
python3 generate_compute_trends.py
```

Use `--formats png svg pdf eps` to select formats. The two provenance CSVs,
device-linked chart-data JSON, and four chart files are written to `55yrs-compute/`.

## Build the interactive Astro page

```sh
cd 55yrs-compute/site
npm install
npm run dev
```

Open the local URL printed by Astro. For a static release, run `npm run build`;
the single page is written to `55yrs-compute/site/dist/`. Re-run
`python3 generate_compute_trends.py` before building after changing chart data.

For a local Cloudflare Workers preview, run `npm run preview`. To publish after
signing in to Cloudflare, run `npm run deploy`. These commands use a local,
Git-ignored `wrangler.jsonc` whose `assets.directory` is `./dist`.
