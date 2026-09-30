#!/usr/bin/env python3
"""Build the historical processor inputs for the uP Trend Chart."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "uptrend" / "legacy-inputs"
PROVENANCE = DATA_DIR / "processor-provenance.json"
PROCESSORS_CSV = DATA_DIR / "legacy-processors.csv"

SERIES = {
    "cores": {
        "order": 0,
        "metric": "logical_threads_or_compute_units",
        "plot_unit": "count",
        "newdata_key": "logical_threads_or_compute_units",
    },
    "frequency": {
        "order": 1,
        "metric": "frequency",
        "plot_unit": "MHz",
        "newdata_key": "frequency_ghz",
    },
    "specint": {
        "order": 2,
        "metric": "single_thread_performance",
        "plot_unit": "SPECint_rate_x1000",
        "newdata_key": "single_thread_performance_specint",
    },
    "transistors": {
        "order": 3,
        "metric": "transistors",
        "plot_unit": "thousand_transistors",
        "newdata_key": "transistors_billion",
    },
    "watts": {
        "order": 4,
        "metric": "typical_power_tdp",
        "plot_unit": "W",
        "newdata_key": "typical_power_tdp_watts",
    },
}

POINT_FIELDS = [
    "point_id", "plot_year", "series", "metric", "plot_value", "plot_unit",
    "source_file", "source_line", "processor_id", "processor_name", "manufacturer",
    "release_year", "release_month", "documented_source_value", "documented_source_unit",
    "source_value_in_plot_units", "validation_status", "match_confidence",
    "provenance_tier", "reference_urls", "notes",
]

PROCESSOR_FIELDS = [
    "processor_id", "processor_name", "manufacturer", "processor_type", "release_year",
    "release_month", "transistors_billion", "single_thread_performance_specint",
    "single_thread_performance_basis", "single_thread_performance_status",
    "single_thread_performance_confidence",
    "frequency_ghz", "typical_power_tdp_watts", "logical_threads_or_compute_units",
    "parallel_unit_basis", "match_confidence", "provenance_tier", "reference_urls",
    "notes", "plot_point_ids",
]

EXPECTED_COUNTS = {
    "cores": 90,
    "frequency": 101,
    "specint": 74,
    "transistors": 99,
    "watts": 101,
}

EXCLUDED_FROM_VALIDATED = {
    "approved_specint_discrepancy",
    "approved_tdp_discrepancy",
}


def is_validated_row(row: dict) -> bool:
    return (
        bool(row["processor_name"])
        and row["match_confidence"] in {"direct", "high"}
        and row["validation_status"] not in EXCLUDED_FROM_VALIDATED
    )


def clean_number(value: str) -> str:
    """Return a stable, non-scientific representation without changing its value."""
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def close_enough(actual: Decimal, expected: Decimal, *, relative: Decimal = Decimal("0.000001")) -> bool:
    if actual == expected:
        return True
    return abs(actual - expected) <= max(Decimal("0.000001"), abs(expected) * relative)


def parse_dat_files() -> tuple[list[dict], dict[str, list[dict]]]:
    all_points: list[dict] = []
    by_series: dict[str, list[dict]] = {}
    for series, meta in SERIES.items():
        path = DATA_DIR / f"{series}.dat"
        points: list[dict] = []
        segment = 0
        with path.open(encoding="utf-8") as handle:
            for source_line, raw in enumerate(handle, 1):
                stripped = raw.strip()
                if not stripped:
                    continue
                if stripped.startswith("####"):
                    segment += 1
                    continue
                body, _, comment = stripped.partition("#")
                fields = body.split()
                if len(fields) != 2:
                    raise ValueError(f"Unexpected data at {path}:{source_line}: {raw.rstrip()}")
                index = len(points) + 1
                point = {
                    "point_id": f"series-{series}-point-{index:03d}",
                    "plot_year": fields[0],
                    "plot_value": fields[1],
                    "series": series,
                    "metric": meta["metric"],
                    "plot_unit": meta["plot_unit"],
                    "source_file": f"uptrend/legacy-inputs/{path.name}",
                    "source_line": source_line,
                    "source_comment": comment.strip(),
                    "segment": segment,
                    "series_index": index,
                }
                points.append(point)
                all_points.append(point)
        by_series[series] = points
    return all_points, by_series


def infer_manufacturer(name: str) -> str:
    lower = name.lower()
    if lower.startswith("xeon") or lower.startswith("core"):
        return "Intel"
    if lower.startswith(("opteron", "epyc", "ryzen", "radeon")):
        return "AMD"
    if lower.startswith("power"):
        return "IBM"
    if lower.startswith("m1"):
        return "Apple"
    if lower.startswith("quadro"):
        return "NVIDIA"
    if lower.startswith("graviton"):
        return "Amazon"
    return ""


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def parse_newdata() -> list[dict]:
    records: list[dict] = []
    path = DATA_DIR / "newdata.txt"
    pattern = re.compile(
        r"^(?P<name>.+?)\s+(?P<date>\d{4}\.\d{2})\s+"
        r"(?P<trans>\S+)\s+(?P<spec>\S+)\s+(?P<freq>\S+)\s+"
        r"(?P<tdp>\S+)\s+(?P<threads>\S+)(?:\s+#\s*(?P<notes>.*))?$"
    )
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            match = pattern.match(raw.strip())
            if not match:
                continue
            values = match.groupdict()
            year, month = values["date"].split(".")
            name = values["name"].strip()
            processor_type = "GPU" if name in {"Quadro GV100", "Radeon MI250"} else (
                "many-core accelerator" if name.startswith("Xeon Phi") else "CPU"
            )
            basis = "SMs" if name == "Quadro GV100" else (
                "compute units" if name == "Radeon MI250" else "logical threads (SMT counted separately)"
            )
            record = {
                "processor_id": f"newdata-{slugify(name)}-{year}-{month}",
                "processor_name": name,
                "manufacturer": infer_manufacturer(name),
                "processor_type": processor_type,
                "release_year": year,
                "release_month": month,
                "transistors_billion": "" if values["trans"] == "??" else clean_number(values["trans"]),
                "single_thread_performance_specint": "" if values["spec"] == "??" else clean_number(values["spec"]),
                "frequency_ghz": "" if values["freq"] == "??" else clean_number(values["freq"]),
                "typical_power_tdp_watts": "" if values["tdp"] == "??" else clean_number(values["tdp"]),
                "logical_threads_or_compute_units": "" if values["threads"] == "??" else clean_number(values["threads"]),
                "parallel_unit_basis": basis,
                "match_confidence": "direct",
                "provenance_tier": "repository_source",
                "reference_urls": [
                    f"https://github.com/karlrupp/microprocessor-trend-data/blob/master/newdata.txt#L{line_number}"
                ],
                "notes": values["notes"] or "",
                "source_line": line_number,
                "source_date": values["date"],
            }
            if record["single_thread_performance_specint"]:
                benchmark = "cpu2017" if int(year) >= 2017 else "cpu2006"
                record["reference_urls"].append(f"https://www.spec.org/{benchmark}/results/")
            record["reference_urls"].extend(
                url.rstrip(".,)") for url in re.findall(r"https?://\S+", record["notes"])
                if url.rstrip(".,)") not in record["reference_urls"]
            )
            if name == "Xeon E5-2699v3":
                record["reference_urls"].append(
                    "https://www.intel.com/content/www/us/en/products/sku/81061/"
                    "intel-xeon-processor-e52699-v3-45m-cache-2-30-ghz/specifications.html"
                )
            records.append(record)
    if len(records) != 31:
        raise ValueError(f"Expected 31 newdata records, found {len(records)}")
    return records


def source_details(processor: dict, series: str) -> tuple[str, str, str]:
    """Return source value, source unit, and value converted to graph units."""
    if processor["origin"] == "newdata":
        key = SERIES[series]["newdata_key"]
        value = processor.get(key, "")
        if not value:
            return "", "", ""
        number = Decimal(value)
        if series == "frequency":
            return value, "GHz", clean_number(str(number * 1000))
        if series == "transistors":
            return value, "billion transistors", clean_number(str(number * 1000000))
        if series == "specint":
            return value, "SPECint rate", clean_number(str(number * 1000))
        if series == "cores":
            return value, processor["parallel_unit_basis"], value
        return value, "W", value

    values = processor.get("source_values", {})
    value = values.get(series, "")
    if not value:
        return "", "", ""
    number = Decimal(value)
    if processor.get("inline_named") and series == "specint":
        return value, "SPECint2006", clean_number(str(number * 1000))
    if series == "transistors":
        return value, "million transistors", clean_number(str(number * 1000))
    if series == "specint":
        return value, "Cornell normalized performance", clean_number(str(number * 1800))
    if series == "frequency":
        return value, "MHz", value
    if series == "cores":
        return value, "cores", value
    return value, "W", value


def build_assignments(points_by_series: dict[str, list[dict]], provenance: dict, newdata: list[dict]) -> tuple[dict, dict]:
    processors: dict[str, dict] = {}
    for processor in provenance["legacy_processors"] + provenance["inline_processors"]:
        item = dict(processor)
        item["origin"] = "legacy"
        processors[item["processor_id"]] = item
    for record in newdata:
        item = dict(record)
        item["origin"] = "newdata"
        processors[item["processor_id"]] = item

    assignments = dict(provenance["legacy_point_matches"])
    for processor in provenance["inline_processors"]:
        assignments[processor["point_id"]] = {
            "processor_id": processor["processor_id"],
            "confidence": "direct",
            "note": "Processor name is embedded in the original specint.dat comment.",
        }

    for series, points in points_by_series.items():
        post_points = [point for point in points if point["segment"] == max(p["segment"] for p in points)]
        records = [record for record in newdata if record[SERIES[series]["newdata_key"]]]
        if len(post_points) != len(records):
            raise ValueError(f"Post-2010 {series} mapping mismatch: {len(post_points)} points, {len(records)} records")
        for point, record in zip(post_points, records):
            assignments[point["point_id"]] = {
                "processor_id": record["processor_id"],
                "confidence": "direct",
                "note": "Mapped by source order to the corresponding populated newdata.txt field.",
            }
    return assignments, processors


def point_rows(points: list[dict], assignments: dict, processors: dict, provenance: dict) -> tuple[list[dict], dict[str, list[str]]]:
    rows: list[dict] = []
    linked: dict[str, list[str]] = defaultdict(list)
    approved_specint_mismatches = 0
    approved_tdp_mismatches = 0
    candidate_notes = provenance.get("unresolved_point_notes", {})

    for point in points:
        row = {field: "" for field in POINT_FIELDS}
        for field in ("point_id", "plot_year", "series", "metric", "plot_value", "plot_unit", "source_file", "source_line"):
            row[field] = point[field]
        assignment = assignments.get(point["point_id"])
        if not assignment:
            row.update({
                "validation_status": "graph_only_unresolved",
                "match_confidence": "unresolved",
                "notes": candidate_notes.get(
                    point["point_id"],
                    "No unique processor identity could be supported by the available original-source fields.",
                ),
            })
            rows.append(row)
            continue

        processor = processors[assignment["processor_id"]]
        linked[processor["processor_id"]].append(point["point_id"])
        row.update({
            "processor_id": processor["processor_id"],
            "processor_name": processor["processor_name"],
            "manufacturer": processor.get("manufacturer", ""),
            "release_year": processor["release_year"],
            "release_month": processor.get("release_month", ""),
            "match_confidence": assignment["confidence"],
            "provenance_tier": processor["provenance_tier"],
            "reference_urls": "; ".join(processor["reference_urls"]),
        })
        source_value, source_unit, converted = source_details(processor, point["series"])
        row["documented_source_value"] = source_value
        row["documented_source_unit"] = source_unit
        row["source_value_in_plot_units"] = converted
        notes = [assignment.get("note", "")]
        if point["source_comment"]:
            notes.append(f"Source comment: {point['source_comment']}")

        actual = Decimal(point["plot_value"])
        expected = Decimal(converted) if converted else None
        if processor["origin"] == "newdata":
            release_coordinate = Decimal(processor["source_date"])
            if Decimal(point["plot_year"]) != release_coordinate:
                notes.append(
                    f"Graph x-coordinate {point['plot_year']} is approximate; source date is {processor['source_date']}."
                )
            if expected is not None and close_enough(actual, expected):
                row["validation_status"] = "unit_converted_match"
            elif point["series"] == "specint":
                row["validation_status"] = "approved_specint_discrepancy"
                approved_specint_mismatches += 1
                notes.append("Known graph/source conflict: the plotted post-2010 value is not source SPECint × 1000.")
            elif processor["processor_name"] == "Xeon E5-2699v3" and point["series"] == "watts" and actual == 140 and expected == 145:
                row["validation_status"] = "approved_tdp_discrepancy"
                approved_tdp_mismatches += 1
                notes.append("Known graph/source conflict: chart is 140 W; newdata.txt and Intel specify 145 W.")
            else:
                raise ValueError(
                    f"Unapproved newdata mismatch for {point['point_id']}: graph={actual}, source={expected}"
                )
        elif expected is None:
            row["validation_status"] = "named_point_no_numeric_source"
        elif processor.get("inline_named"):
            if close_enough(actual, expected):
                row["validation_status"] = "inline_named_match"
            else:
                row["validation_status"] = "digitized_source_match"
                notes.append("The named graph point differs slightly from the cited official benchmark result; both values are preserved.")
        else:
            row["validation_status"] = "digitized_source_match"
        row["notes"] = " ".join(note for note in notes if note)
        rows.append(row)

    if approved_specint_mismatches != 15:
        raise ValueError(f"Expected 15 approved SPECint mismatches, found {approved_specint_mismatches}")
    if approved_tdp_mismatches != 1:
        raise ValueError(f"Expected one approved TDP mismatch, found {approved_tdp_mismatches}")
    rows.sort(key=lambda row: (Decimal(row["plot_year"]), SERIES[row["series"]]["order"], int(row["source_line"])))
    return rows, linked


def single_thread_performance(processor: dict, provenance: dict) -> dict[str, object]:
    value = processor.get("single_thread_performance_specint", "")
    if value:
        if processor["origin"] == "newdata":
            if int(processor["release_year"]) >= 2017:
                return {
                    "value": value,
                    "basis": "SPECspeed2017_int multiplied by 9",
                    "status": "suite_converted",
                    "confidence": "high",
                    "reference_urls": [],
                    "notes": "The repository converts SPEC CPU2017 integer speed to its SPECint2006-equivalent scale with a factor of 9.0.",
                }
            return {
                "value": value,
                "basis": "SPECint2006",
                "status": "source_reported",
                "confidence": "direct",
                "reference_urls": [],
                "notes": "",
            }
        return {
            "value": value,
            "basis": "SPECint2006",
            "status": "source_reported",
            "confidence": "direct",
            "reference_urls": [],
            "notes": "",
        }

    legacy_value = processor.get("source_values", {}).get("specint", "")
    if processor["origin"] == "legacy" and legacy_value:
        pre_spec = int(processor["release_year"]) < 1990
        return {
            "value": clean_number(legacy_value),
            "basis": "Stanford/Cornell normalized SPECint2006-equivalent performance",
            "status": "inferred_pre_spec_proxy" if pre_spec else "source_reported_normalized",
            "confidence": "medium" if pre_spec else "high",
            "reference_urls": [],
            "notes": (
                "The original-author lineage flags pre-1990 values as performance proxies rather than true SPECint results."
                if pre_spec else
                "Copied from the Cornell normalized-performance field, which follows the Stanford cross-suite SPECint normalization method."
            ),
        }

    supplemental = provenance.get("supplemental_single_thread_performance", {}).get(
        processor["processor_id"]
    )
    if supplemental:
        return {
            "value": supplemental["value"],
            "basis": supplemental["basis"],
            "status": supplemental["status"],
            "confidence": supplemental["confidence"],
            "reference_urls": supplemental["reference_urls"],
            "notes": supplemental["notes"],
        }
    return {
        "value": "",
        "basis": "",
        "status": "unresolved",
        "confidence": "unresolved",
        "reference_urls": [],
        "notes": "No defensible single-thread SPECint value or conversion was found.",
    }


def processor_rows(
    processors: dict,
    linked: dict[str, list[str]],
    provenance: dict,
) -> list[dict]:
    rows: list[dict] = []
    for processor_id, point_ids in linked.items():
        processor = processors[processor_id]
        row = {field: "" for field in PROCESSOR_FIELDS}
        for field in PROCESSOR_FIELDS:
            if field in processor and field not in {"reference_urls", "notes"}:
                row[field] = processor[field]
        performance = single_thread_performance(processor, provenance)
        row["single_thread_performance_specint"] = performance["value"]
        row["single_thread_performance_basis"] = performance["basis"]
        row["single_thread_performance_status"] = performance["status"]
        row["single_thread_performance_confidence"] = performance["confidence"]
        references = list(processor["reference_urls"])
        references.extend(
            url for url in performance["reference_urls"] if url not in references
        )
        row["reference_urls"] = "; ".join(references)
        notes = [processor.get("notes", ""), str(performance["notes"])]
        row["notes"] = " ".join(note for note in notes if note)
        row["plot_point_ids"] = "; ".join(sorted(point_ids))
        rows.append(row)
    rows.sort(key=lambda row: (
        int(row["release_year"]), int(row["release_month"] or 0), row["processor_name"].lower(), row["processor_id"]
    ))
    return rows


def validate(
    points: list[dict],
    point_rows_: list[dict],
    validated_rows: list[dict],
    processor_rows_: list[dict],
    provenance: dict,
) -> None:
    if len(points) != 465 or len(point_rows_) != 465:
        raise ValueError(f"Expected 465 point rows, found {len(point_rows_)}")
    counts = defaultdict(int)
    for row in point_rows_:
        counts[row["series"]] += 1
    if dict(counts) != EXPECTED_COUNTS:
        raise ValueError(f"Unexpected series counts: {dict(counts)}")
    ids = [row["point_id"] for row in point_rows_]
    if len(ids) != len(set(ids)):
        raise ValueError("Point IDs are not unique")
    original = {point["point_id"]: point for point in points}
    for row in point_rows_:
        point = original[row["point_id"]]
        if row["plot_year"] != point["plot_year"] or row["plot_value"] != point["plot_value"]:
            raise ValueError(f"Round-trip failure for {row['point_id']}")
        if row["processor_name"] and not (row["match_confidence"] and row["provenance_tier"] and row["reference_urls"]):
            raise ValueError(f"Incomplete provenance for {row['point_id']}")
        if not row["processor_name"] and row["validation_status"] != "graph_only_unresolved":
            raise ValueError(f"Unnamed point lacks unresolved status: {row['point_id']}")
        if "??" in ",".join(str(row[field]) for field in POINT_FIELDS):
            raise ValueError(f"Placeholder found in {row['point_id']}")
    expected_order = sorted(
        point_rows_, key=lambda row: (Decimal(row["plot_year"]), SERIES[row["series"]]["order"], int(row["source_line"]))
    )
    if point_rows_ != expected_order:
        raise ValueError("Point ledger is not chronologically sorted")
    expected_validated = [row for row in point_rows_ if is_validated_row(row)]
    if validated_rows != expected_validated:
        raise ValueError("Validated-only export does not exactly match the approved confidence filter")
    if any(row["match_confidence"] not in {"direct", "high"} for row in validated_rows):
        raise ValueError("Validated-only export contains a non-approved confidence value")
    if len({row["point_id"] for row in validated_rows}) != len(validated_rows):
        raise ValueError("Validated-only point IDs are not unique")
    point_id_set = set(ids)
    for row in processor_rows_:
        linked = [value.strip() for value in row["plot_point_ids"].split(";") if value.strip()]
        if not linked or any(point_id not in point_id_set for point_id in linked):
            raise ValueError(f"Invalid processor links for {row['processor_id']}")
        performance_value = row["single_thread_performance_specint"]
        if performance_value and not (
            row["single_thread_performance_basis"]
            and row["single_thread_performance_status"]
            and row["single_thread_performance_confidence"]
            and row["reference_urls"]
        ):
            raise ValueError(f"Incomplete performance provenance for {row['processor_id']}")
        if not performance_value and not row["single_thread_performance_status"].startswith(
            ("unresolved", "not_applicable")
        ):
            raise ValueError(f"Missing performance status for {row['processor_id']}")
    missing_performance = [
        row for row in processor_rows_ if not row["single_thread_performance_specint"]
    ]
    expected_unavailable = {
        processor_id
        for processor_id, performance in provenance.get(
            "supplemental_single_thread_performance", {}
        ).items()
        if not performance["value"]
    }
    actual_unavailable = {row["processor_id"] for row in missing_performance}
    if actual_unavailable != expected_unavailable:
        raise ValueError(
            "Unexpected unavailable single-thread values: "
            f"missing={sorted(actual_unavailable - expected_unavailable)}, "
            f"unexpectedly populated={sorted(expected_unavailable - actual_unavailable)}"
        )
    named_newdata = {
        row["processor_name"] for row in point_rows_ if row["processor_id"].startswith("newdata-")
    }
    if len(named_newdata) != 31:
        raise ValueError(f"Expected all 31 newdata processors, found {len(named_newdata)}")
    inline = [row for row in point_rows_ if "Source comment:" in row["notes"]]
    if len(inline) != 5:
        raise ValueError(f"Expected five inline-named SPECint points, found {len(inline)}")


def write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate generated content without replacing CSV files")
    args = parser.parse_args()
    with PROVENANCE.open(encoding="utf-8") as handle:
        provenance = json.load(handle)
    points, points_by_series = parse_dat_files()
    newdata = parse_newdata()
    assignments, processors = build_assignments(points_by_series, provenance, newdata)
    ledger, linked = point_rows(points, assignments, processors, provenance)
    processor_view = processor_rows(processors, linked, provenance)
    validated = [row for row in ledger if is_validated_row(row)]
    validate(points, ledger, validated, processor_view, provenance)
    if not args.check:
        write_csv_atomic(PROCESSORS_CSV, PROCESSOR_FIELDS, processor_view)
    unresolved = sum(row["match_confidence"] == "unresolved" for row in ledger)
    print(
        f"Validated {len(ledger)} chart points, {len(validated)} approved point rows, and "
        f"{len(processor_view)} processor rows ({unresolved} unresolved point identities)."
    )


if __name__ == "__main__":
    main()
