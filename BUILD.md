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
