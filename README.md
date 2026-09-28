# Uptrends

This repository extends [Karl Rupp's Microprocessor Trend Data](https://github.com/karlrupp/microprocessor-trend-data) with reproducible chart generation, validated processor data, and CPU and GPU compute trends. It is maintained as an independent project. Explore the charts at [uptrends.eelab.dev](https://uptrends.eelab.dev/).

## What's here

- The historical 40-, 42-, 48-, and 50-year processor datasets and charts, with editable PNG, SVG, PDF, and EPS outputs.
- [50-year processor exports](50yrs/50-years-processors.csv), a [point-by-point provenance ledger](50yrs/processor-provenance.json), and a [verified-only chart](50yrs-verified/50-years-verified-processor-trend.png).
- A [55-year CPU and GPU compute-trends edition](55yrs-compute/METHODOLOGY.md) with device and observation CSVs, cited source data, charts, and an [interactive site](55yrs-compute/site/).

See [BUILD.md](BUILD.md) for requirements and commands to regenerate the charts and run the interactive site.

## Origin and attribution

Karl Rupp created the [original repository](https://github.com/karlrupp/microprocessor-trend-data) and its historical processor datasets and charts for his [microprocessor trend series](https://www.karlrupp.net/2018/02/42-years-of-microprocessor-trend-data/). This project retains that work and its Git history. The original charts also credit M. Horowitz, F. Labonte, O. Shacham, K. Olukotun, L. Hammond, and C. Batten for data through 2010.

This project adds multi-format chart generation, processor-level CSV exports and provenance checks, a verified-only 50-year edition, and a 55-year CPU/GPU compute-trends edition with an interactive visualization. It is independent of, and is not endorsed by, the original author.

## License

The original material and this project's additions are available under the [Creative Commons Attribution 4.0 International license](LICENSE.txt) ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)). When reusing the material, credit the original and subsequent contributors, link to the source and license, and indicate changes.
