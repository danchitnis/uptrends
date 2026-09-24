#!/usr/bin/env python3
"""Generate the provenance CSVs and overlaid CPU/GPU compute-trends chart."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
import reportlab
import compute_chart


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "55yrs-compute"
PROVENANCE = OUT_DIR / "compute-provenance.json"
APPLE_SINGLE_CORE = OUT_DIR / "apple-single-core-geekbench7.json"
APPLE_SUPPLEMENTAL = OUT_DIR / "apple-supplemental-metrics.json"
PHYSICAL_CORE_OVERRIDES = OUT_DIR / "physical-core-overrides.json"
MULTICORE_RESULTS = OUT_DIR / "cpu-multicore-results.json"
HISTORICAL_MULTICORE_RESULTS = OUT_DIR / "cpu-multicore-historical-results.json"
THROUGHPUT_BRIDGES = OUT_DIR / "cpu-throughput-bridges.json"
THROUGHPUT_INDEX = "cpu_multicore_index"
# MI300A is a mixed CPU/GPU package; only its FP32 observation is GPU work.
GPU_APU_FP32_IDS = frozenset({"amd-instinct-mi300a"})
MULTICORE_SUITES = {
    "SPECint_rate_base2000": ("cpu_multicore_rate2000_int_base", "cpu2000"),
    "SPECint_rate_base2006": ("cpu_multicore_rate2006_int_base", "cpu2006"),
    "SPECrate2017_int_base": ("cpu_multicore_rate2017_int_base", "cpu2017"),
}
MULTICORE_METRICS = tuple(metric for metric, _ in MULTICORE_SUITES.values())
# These historical rows are not comparable general-purpose CPU-package core
# counts: Cell includes heterogeneous SPEs, Rock was not a shipping product,
# and Xeon Phi belongs to the separate many-core accelerator class.
CORE_COUNT_EXCLUSIONS = {
    "cornell-ibm-cell-2007-05-05",
    "cornell-sun-rock-2009-09-01",
    "newdata-xeon-phi-2013-05",
    "newdata-xeon-phi-7290-2016-06",
}
DEVICES_CSV = OUT_DIR / "55-years-compute-devices.csv"
OBSERVATIONS_CSV = OUT_DIR / "55-years-compute-observations.csv"
CHART_DATA_JSON = OUT_DIR / "55-years-compute-chart-data.json"
BASE_NAME = "55-years-processor-compute-trends"
FORMATS = ("png", "svg", "pdf", "eps")
WIDTH = 1800
HEIGHT = 2050
POINT_OPACITY = 0.78
SVG_NS = "http://www.w3.org/2000/svg"
FONT_STACK = "Arial, Helvetica, sans-serif"
METRICS = (
    "transistors_million",
    "frequency_ghz",
    "power_w",
    "cpu_physical_cores",
    "gpu_compute_units",
    "cpu_single_thread_normalized",
    "cpu_multicore_rate2000_int_base",
    "cpu_multicore_rate2006_int_base",
    "cpu_multicore_rate2017_int_base",
    THROUGHPUT_INDEX,
    "fp32_dense_peak_gflops",
)
UNITS = {
    "transistors_million": "million transistors",
    "frequency_ghz": "GHz",
    "power_w": "W",
    "cpu_physical_cores": "physical cores",
    "gpu_compute_units": "SMs / CUs / GPU cores",
    "cpu_single_thread_normalized": "normalized single-core index (legacy SPEC scale)",
    "cpu_multicore_rate2000_int_base": "SPECint_rate_base2000 (one chip)",
    "cpu_multicore_rate2006_int_base": "SPECint_rate_base2006 (one chip)",
    "cpu_multicore_rate2017_int_base": "SPECrate2017_int_base (one chip)",
    THROUGHPUT_INDEX: "estimated one-chip throughput index (2003=100)",
    "fp32_dense_peak_gflops": "GFLOP/s",
}
PANEL_METRICS = {
    "transistors": ("transistors_million",),
    "frequency": ("frequency_ghz",),
    "power": ("power_w",),
    "parallelism": ("cpu_physical_cores", "gpu_compute_units"),
    "single-thread": ("cpu_single_thread_normalized",),
    "multi-core": (*MULTICORE_METRICS, THROUGHPUT_INDEX),
    "fp32": ("fp32_dense_peak_gflops",),
}
SERIES_COLOURS = {
    "cpu": "#2468B4",
    "soc": "#008B8B",
    "integrated-gpu": "#7A4EB3",
    "discrete-gpu": "#D06B14",
    "manycore": "#B33B72",
    "cpu-cores": "#2468B4",
    "gpu-units": "#D06B14",
    "single-thread": "#3154A4",
}
SERIES_LABELS = {
    "cpu": "CPU",
    "soc": "SoC",
    "integrated-gpu": "Integrated GPU",
    "discrete-gpu": "Discrete GPU",
    "manycore": "Many-core / APU",
    "cpu-cores": "CPU physical cores",
    "gpu-units": "GPU SM / CU / GPU cores",
    "single-thread": "CPU single-thread performance",
}


@dataclass
class Device:
    device_id: str
    name: str
    vendor: str
    device_class: str
    market: str
    package_kind: str
    release_date: str
    shipping_status: str
    source_tier: str
    references: list[str]
    notes: str
    metrics: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Observation:
    observation_id: str
    device_id: str
    device_name: str
    vendor: str
    market: str
    device_class: str
    component_type: str
    release_date: str
    release_year: float
    metric: str
    value: float | None
    unit: str
    scope: str
    value_kind: str
    confidence: str
    source_tier: str
    references: tuple[str, ...]
    raw_benchmark_suite: str
    raw_benchmark_value: str
    normalization: str
    precision: str
    clock_basis: str
    calculation_operands: str
    calculation_expression: str
    validation_status: str
    notes: str

    @property
    def panel(self) -> str:
        return next(panel for panel, metrics in PANEL_METRICS.items() if self.metric in metrics)

    @property
    def series_key(self) -> str:
        if self.metric == "cpu_physical_cores":
            return "cpu-cores"
        if self.metric == "gpu_compute_units":
            return "gpu-units"
        if self.metric == "cpu_single_thread_normalized":
            return "single-thread"
        if self.metric == THROUGHPUT_INDEX:
            return "multi-core-index"
        if self.metric in MULTICORE_METRICS:
            return "multi-core-" + self.metric.split("rate", 1)[1][:4]
        if self.metric == "fp32_dense_peak_gflops":
            if self.device_class == "gpu":
                return "discrete-gpu"
            if self.device_class == "soc":
                return "integrated-gpu"
            if self.device_class == "manycore":
                return "manycore"
            return "cpu"
        if self.device_class == "gpu":
            return "discrete-gpu"
        return self.device_class if self.device_class in {"cpu", "soc", "manycore"} else "cpu"

    @property
    def gpu_component(self) -> bool:
        return (
            self.device_class == "gpu"
            or self.component_type in {"discrete_gpu", "integrated_gpu"}
            or (
                self.device_id in GPU_APU_FP32_IDS
                and self.metric == "fp32_dense_peak_gflops"
            )
        )

    @property
    def filled(self) -> bool:
        return self.gpu_component


@dataclass(frozen=True)
class Panel:
    key: str
    title: str
    unit_label: str
    y_min: float
    y_max: float


PANELS = (
    Panel("transistors", "Transistor count", "million transistors", 1e-6, 3e5),
    Panel("frequency", "Clock frequency", "GHz", 1e-4, 1e1),
    Panel("power", "Power", "W (scope varies)", 1e-1, 2e3),
    Panel("parallelism", "Parallel resources", "count (non-equivalent)", 7e-1, 1e3),
    Panel("single-thread", "CPU single-thread performance", "normalized single-core index", 1e-5, 3e2),
    Panel("fp32", "Dense FP32 vector peak", "GFLOP/s", 1e2, 3e5),
)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def release_coordinate(iso_date: str) -> float:
    year, month, day = map(int, iso_date.split("-"))
    return year + ((month - 1) * 30.4375 + day - 1) / 365.25


def metric_scope(metric: str, device_class: str) -> str:
    if metric == "power_w":
        return {"gpu": "GPU TBP/TDP", "soc": "SoC package power", "manycore": "package power"}.get(
            device_class, "CPU TDP"
        )
    if metric == "frequency_ghz":
        return "nominal/base clock unless source states otherwise"
    if metric == "gpu_compute_units":
        return "vendor-native GPU unit count; SM, CU, or Apple GPU core"
    if metric == "cpu_physical_cores":
        return "physical CPU cores"
    if metric == "transistors_million":
        return "device or multi-die package transistor count"
    if metric == "cpu_single_thread_normalized":
        return "one-copy benchmark normalized by the documented suite-specific continuity rule"
    if metric == THROUGHPUT_INDEX:
        return "estimated integer-throughput index derived from a measured one-CPU-package SPEC rate"
    if metric in MULTICORE_METRICS:
        return "measured integer throughput of one CPU package in a published system"
    if metric == "fp32_dense_peak_gflops":
        return "dense non-tensor non-sparse FP32 vector peak"
    return "device/package"


def component_type(metric: str, device_class: str) -> str:
    if metric in {"gpu_compute_units", "fp32_dense_peak_gflops"}:
        if device_class == "soc":
            return "integrated_gpu"
        if device_class == "gpu":
            return "discrete_gpu"
        if device_class == "manycore":
            return "manycore"
    if device_class == "soc":
        return "soc_package" if metric in {"transistors_million", "frequency_ghz", "power_w"} else "cpu"
    return device_class


def normalise_metric(value: Any, references: list[str], notes: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result = dict(value)
    else:
        result = {"value": value, "value_kind": "direct", "confidence": "direct"}
    result.setdefault("value_kind", "direct")
    result.setdefault("confidence", "direct")
    result.setdefault("references", list(references))
    result.setdefault("source_tier", "primary")
    result.setdefault("notes", notes)
    return result


def parse_legacy(path: Path) -> dict[str, Device]:
    devices: dict[str, Device] = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            release_date = f"{row['release_year']}-{(row['release_month'] or '01').zfill(2)}-01"
            refs = [item.strip() for item in row["reference_urls"].split(";") if item.strip()]
            device_class = row["processor_type"].strip().lower()
            if device_class not in {"cpu", "gpu", "soc", "manycore"}:
                device_class = "cpu"
            metrics: dict[str, dict[str, Any]] = {}
            def add(metric: str, raw: str, *, kind: str = "direct", confidence: str = "high") -> None:
                if raw.strip():
                    metrics[metric] = normalise_metric(
                        {"value": float(raw), "value_kind": kind, "confidence": confidence},
                        refs,
                        row["notes"],
                    )
            if row["transistors_billion"].strip():
                add("transistors_million", str(float(row["transistors_billion"]) * 1000))
            add("frequency_ghz", row["frequency_ghz"])
            add("power_w", row["typical_power_tdp_watts"])
            units = row["logical_threads_or_compute_units"]
            basis = row["parallel_unit_basis"].lower()
            if units.strip():
                if device_class == "gpu" or "compute unit" in basis or basis == "sms":
                    add("gpu_compute_units", units)
                elif "logical" not in basis and "smt" not in basis:
                    add("cpu_physical_cores", units)
            if row["single_thread_performance_specint"].strip():
                metrics["cpu_single_thread_normalized"] = normalise_metric(
                    {
                        "value": float(row["single_thread_performance_specint"]),
                        "value_kind": "normalized",
                        "confidence": row["single_thread_performance_confidence"] or "medium",
                        "normalization": row["single_thread_performance_basis"],
                    },
                    refs,
                    row["notes"],
                )
            devices[row["processor_id"]] = Device(
                device_id=row["processor_id"],
                name=row["processor_name"],
                vendor=row["manufacturer"],
                device_class=device_class,
                market="historical processor dataset",
                package_kind="processor/package",
                release_date=release_date,
                shipping_status="shipping",
                source_tier=row["provenance_tier"],
                references=refs,
                notes=row["notes"],
                metrics=metrics,
            )
    return devices


def add_throughput_index(devices: dict[str, Device]) -> None:
    """Bridge the three incompatible SPEC suites as an explicitly estimated index."""
    calibration = json.loads(THROUGHPUT_BRIDGES.read_text(encoding="utf-8"))
    expected_pairs = (
        ("SPECint_rate_base2000", "SPECint_rate_base2006"),
        ("SPECint_rate_base2006", "SPECrate2017_int_base"),
    )
    if len(calibration["bridges"]) != 2:
        raise RuntimeError("throughput index requires two reviewed suite bridges")
    factors: list[float] = []
    bridge_urls: set[str] = set()
    for bridge, expected in zip(calibration["bridges"], expected_pairs):
        if (bridge["from"], bridge["to"]) != expected or len(bridge["pairs"]) < 2:
            raise RuntimeError(f"throughput bridge has insufficient matched results: {expected}")
        ratios: list[float] = []
        for pair in bridge["pairs"]:
            old, new = float(pair["old_score"]), float(pair["new_score"])
            if old <= 0 or new <= 0 or not pair.get("processor"):
                raise RuntimeError("throughput bridge contains an invalid result")
            for url in (pair["old_url"], pair["new_url"]):
                if not re.match(r"https://(?:www\.|open\.|ftp\.)?spec\.org/", url):
                    raise RuntimeError("throughput bridge must cite official SPEC reports")
                bridge_urls.add(url)
            ratios.append(new / old)
        if max(ratios) / min(ratios) > 1.2:
            raise RuntimeError(f"throughput bridge ratios diverge by more than 20%: {expected}")
        factors.append(math.exp(statistics.median(math.log(ratio) for ratio in ratios)))
    base_id = calibration["base_device_id"]
    base_suite = calibration["base_suite"]
    base_metric = MULTICORE_SUITES[base_suite][0]
    base_score = float(calibration["base_score"])
    base_index = float(calibration["base_index"])
    if float(devices[base_id].metrics[base_metric]["value"]) != base_score or base_index != 100:
        raise RuntimeError("throughput index base result changed")
    denominators = {
        "SPECint_rate_base2000": base_score,
        "SPECint_rate_base2006": base_score * factors[0],
        "SPECrate2017_int_base": base_score * factors[0] * factors[1],
    }
    for device in devices.values():
        raw = [(suite, device.metrics[metric]) for suite, (metric, _) in MULTICORE_SUITES.items()
               if metric in device.metrics]
        if len(raw) > 1:
            raise RuntimeError(f"multiple suite results need a selected reference: {device.device_id}")
        if not raw:
            continue
        suite, source = raw[0]
        score = float(source["value"])
        index = base_index * score / denominators[suite]
        device.metrics[THROUGHPUT_INDEX] = {
            "value": index,
            "value_kind": "normalized",
            "confidence": "medium",
            "source_tier": "derived_from_primary",
            "references": sorted(set(source["references"]) | bridge_urls),
            "raw_benchmark_suite": suite,
            "raw_benchmark_value": score,
            "normalization": "Educational index; POWER4+ 2003 one-chip SPECint_rate_base2000 15.2 = 100. Bridges use median log ratios of matched one-chip processor results; not an official SPEC conversion.",
            "calculation_operands": {
                "raw_score": score, "base_score": base_score, "base_index": base_index,
                "cpu2000_to_cpu2006": factors[0], "cpu2006_to_cpu2017": factors[1],
                "denominator_for_suite": denominators[suite],
            },
            "calculation_expression": "base_index * raw_score / denominator_for_suite",
            "notes": "Estimated cross-suite index from measured one-chip rate. Raw result and all bridge reports are retained; consult cpu-throughput-bridges.json. SPEC does not endorse cross-suite score conversion.",
        }


def add_apple_single_core(devices: dict[str, Device]) -> None:
    """Keep the measured Geekbench score and its approximate M1-anchored plot value."""
    payload = json.loads(APPLE_SINGLE_CORE.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported Apple single-core provenance schema")
    anchor_id = payload["anchor_device_id"]
    anchor_value = float(payload["anchor_normalized_value"])
    anchor_score = float(payload["anchor_geekbench7_score"])
    if (anchor_id not in devices or anchor_score <= 0
            or float(devices[anchor_id].metrics["cpu_single_thread_normalized"]["value"]) != anchor_value):
        raise RuntimeError("the original M1 single-core anchor changed")
    rows = payload["results"]
    row_ids = [row["device_id"] for row in rows]
    apple_ids = {device.device_id for device in devices.values()
                 if device.vendor == "Apple" and re.fullmatch(r"Apple M\d.*", device.name)}
    if len(row_ids) != len(set(row_ids)) or set(row_ids) != apple_ids:
        raise RuntimeError("Apple M-series single-core coverage is incomplete or duplicated")
    for row in rows:
        device = devices[row["device_id"]]
        score = row["score"]
        evidence = row["evidence"]
        reference = row.get("reference_url", payload["aggregate_reference_url"])
        if (not isinstance(score, int) or score <= 0 or not row.get("system")
                or evidence not in {"aggregate", "single_run"}
                or (evidence == "single_run" and not row.get("reference_url"))
                or not reference.startswith("https://browser.geekbench.com/")):
            raise RuntimeError(f"invalid Apple Geekbench evidence: {device.device_id}")
        normalized = round(anchor_value * score / anchor_score, 1)
        evidence_note = (
            "Geekbench's Mac-chart average, requiring at least five unique submissions"
            if evidence == "aggregate" else "one identified Geekbench Browser submission, not an aggregate"
        )
        device.metrics["cpu_single_thread_normalized"] = normalise_metric({
            "value": normalized,
            "value_kind": "normalized",
            "confidence": "medium",
            "source_tier": "derived_from_benchmark_and_legacy_anchor",
            "references": list(dict.fromkeys([
                reference, payload["anchor_reference_url"], *device.references,
            ])),
            "raw_benchmark_suite": payload["benchmark"],
            "raw_benchmark_value": score,
            "normalization": "59.9 * Geekbench 7 single-core score / 2190 (M1 anchor); approximate index, not a SPEC result",
            "calculation_operands": {
                "raw_score": score,
                "anchor_raw_score": anchor_score,
                "anchor_index": anchor_value,
            },
            "calculation_expression": "round(anchor_index * raw_score / anchor_raw_score, 1)",
            "notes": (
                f"{row['system']}; {evidence_note}. Recorded {payload['snapshot_date']}. "
                "The M1 anchor is the legacy graph's estimated SPECint continuity value; "
                "the cross-benchmark ratio is an illustrative trend estimate, not an official SPEC conversion."
            ),
        }, device.references)


def load_devices() -> tuple[list[Device], str]:
    payload = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    cutoff = payload["cutoff_date"]
    legacy = (PROVENANCE.parent / payload["legacy_processor_csv"]).resolve()
    devices = parse_legacy(legacy)
    sources: dict[str, str] = payload["sources"]
    for item in payload["devices"]:
        refs = [sources[source_id] for source_id in item["source_ids"]]
        metrics = {
            metric: normalise_metric(value, refs)
            for metric, value in item.get("metrics", {}).items()
        }
        existing = devices.get(item["device_id"])
        if existing:
            existing.name = item["name"]
            existing.vendor = item["vendor"]
            existing.device_class = item["device_class"]
            existing.market = item["market"]
            existing.package_kind = item["package_kind"]
            existing.release_date = item["release_date"]
            existing.references = list(dict.fromkeys(existing.references + refs))
            existing.metrics.update(metrics)
            existing.source_tier = "primary+legacy"
        else:
            devices[item["device_id"]] = Device(
                device_id=item["device_id"],
                name=item["name"],
                vendor=item["vendor"],
                device_class=item["device_class"],
                market=item["market"],
                package_kind=item["package_kind"],
                release_date=item["release_date"],
                shipping_status="shipping",
                source_tier="primary",
                references=refs,
                notes=item.get("notes", ""),
                metrics=metrics,
            )
    add_apple_single_core(devices)
    supplemental = json.loads(APPLE_SUPPLEMENTAL.read_text(encoding="utf-8"))
    for item in supplemental["devices"]:
        device = devices.get(item["device_id"])
        if device is None or device.vendor != "Apple":
            raise RuntimeError(f"supplemental Apple metric has no matching device: {item['device_id']}")
        for metric, raw in item["metrics"].items():
            if metric in device.metrics:
                raise RuntimeError(f"supplemental Apple metric would overwrite provenance: {item['device_id']} {metric}")
            if raw.get("value") is None or not raw.get("references"):
                raise RuntimeError(f"supplemental Apple metric lacks value or citation: {item['device_id']} {metric}")
            device.metrics[metric] = normalise_metric(raw, device.references)
    overrides = json.loads(PHYSICAL_CORE_OVERRIDES.read_text(encoding="utf-8"))
    for item in overrides["devices"]:
        device = devices.get(item["device_id"])
        if device is None:
            raise RuntimeError(f"physical-core override has no matching device: {item['device_id']}")
        detail = normalise_metric(
            {
                "value": item["physical_cores"],
                "value_kind": item["value_kind"],
                "confidence": item["confidence"],
                "notes": item["notes"],
                "references": item.get("reference_urls", device.references),
                "source_tier": item.get("source_tier", device.source_tier),
            },
            device.references,
        )
        device.metrics["cpu_physical_cores"] = detail
    historical = json.loads(HISTORICAL_MULTICORE_RESULTS.read_text(encoding="utf-8"))
    for item in historical.get("additional_devices", []):
        device_id = item["device_id"]
        if device_id in devices:
            raise RuntimeError(f"duplicate historical device: {device_id}")
        reference = item["reference_url"]
        devices[device_id] = Device(
            device_id=device_id, name=item["name"], vendor=item["vendor"],
            device_class="cpu", market=item["market"], package_kind="one CPU chip",
            release_date=item["release_date"], shipping_status="shipping",
            source_tier="primary", references=[reference],
            notes="Release date uses the official SPEC report's hardware-availability month.",
            metrics={"cpu_physical_cores": normalise_metric(
                {"value": item["physical_cores"], "value_kind": "direct", "confidence": "direct",
                 "references": [reference], "source_tier": "primary"}, [reference])},
        )
    historical_payloads = [
        {"benchmark": suite, "results": [item for item in historical["results"] if item["benchmark"] == suite]}
        for suite in ("SPECint_rate_base2000", "SPECint_rate_base2006")
    ]
    for payload in (*historical_payloads, json.loads(MULTICORE_RESULTS.read_text(encoding="utf-8"))):
        benchmark = payload.get("benchmark")
        if benchmark not in MULTICORE_SUITES:
            raise RuntimeError(f"unsupported multi-core benchmark: {benchmark}")
        metric, suite_path = MULTICORE_SUITES[benchmark]
        seen_rate_devices: set[str] = set()
        for item in payload["results"]:
            device_id = item["device_id"]
            if device_id in seen_rate_devices or device_id not in devices:
                raise RuntimeError(f"duplicate or unknown multi-core result device: {device_id}")
            seen_rate_devices.add(device_id)
            device = devices[device_id]
            if device.device_class == "gpu" or item.get("chips") != 1:
                raise RuntimeError(f"multi-core rate must measure exactly one CPU chip: {device_id}")
            if not isinstance(item.get("value"), (int, float)) or not math.isfinite(item["value"]) or item["value"] <= 0:
                raise RuntimeError(f"multi-core rate must be a positive finite result: {device_id}")
            if not item.get("system") or (payload in historical_payloads and not item.get("hardware_availability")):
                raise RuntimeError(f"multi-core result lacks tested system or hardware availability: {device_id}")
            if metric in device.metrics:
                raise RuntimeError(f"duplicate multi-core metric: {device_id}")
            reference = item["reference_url"]
            if not re.fullmatch(rf"https://(?:www\.|open\.|ftp\.)?spec\.org/{suite_path}/results/.+\.(?:html|pdf)", reference):
                raise RuntimeError(f"multi-core result must cite the official SPEC report: {device_id}")
            device.metrics[metric] = {
                "value": item["value"], "value_kind": "measured", "confidence": "direct",
                "source_tier": "primary", "references": [reference],
                "raw_benchmark_suite": benchmark, "raw_benchmark_value": item["value"],
                "notes": (f"Published one-chip system result: {item['system']}; hardware availability "
                          f"{item.get('hardware_availability', 'not recorded')}. Score belongs to this tested configuration "
                          "and this SPEC suite only; scores from other suites are not comparable."),
            }
    # Hardware-thread counts may remain in the source provenance for reference,
    # but they are deliberately absent from generated observations and charts.
    for device in devices.values():
        device.metrics.pop("cpu_hardware_threads", None)
    add_throughput_index(devices)
    ordered = sorted(devices.values(), key=lambda item: (item.release_date, item.name, item.device_id))
    return ordered, cutoff


def build_observations(devices: list[Device]) -> list[Observation]:
    result: list[Observation] = []
    for device in devices:
        for metric in METRICS:
            detail = device.metrics.get(metric)
            value = float(detail["value"]) if detail and detail.get("value") not in (None, "") else None
            kind = detail.get("value_kind", "unresolved") if detail else "unresolved"
            confidence = detail.get("confidence", "unresolved") if detail else "unresolved"
            references = tuple(detail.get("references", device.references)) if detail else tuple()
            status = "eligible" if value is not None else "unresolved"
            if value is None and device.device_class == "gpu" and metric in {
                "cpu_physical_cores", "cpu_single_thread_normalized", *MULTICORE_METRICS, THROUGHPUT_INDEX
            }:
                status = "not_applicable_gpu"
            if value is None and device.device_class in {"cpu", "manycore"} and metric == "gpu_compute_units":
                status = "not_applicable_cpu"
            if value is not None and metric == "gpu_compute_units":
                status = "supporting_noncomparable_not_plotted"
            if value is not None and metric == "cpu_physical_cores" and (
                device.device_class not in {"cpu", "soc"} or device.device_id in CORE_COUNT_EXCLUSIONS
            ):
                status = "supporting_noncomparable_not_plotted"
            if value is not None and metric in MULTICORE_METRICS:
                status = "supporting_raw_benchmark_not_plotted"
            if (
                value is not None
                and metric == "cpu_single_thread_normalized"
                and device.release_date < "1988-01-01"
                and any(
                    word in (detail.get("notes", device.notes) if detail else device.notes).lower()
                    for word in ("proxy", "proxies")
                )
            ):
                status = "supporting_proxy_not_plotted"
            result.append(
                Observation(
                    observation_id=f"obs-{slug(metric)}-{device.device_id}",
                    device_id=device.device_id,
                    device_name=device.name,
                    vendor=device.vendor,
                    market=device.market,
                    device_class=device.device_class,
                    component_type=component_type(metric, device.device_class),
                    release_date=device.release_date,
                    release_year=release_coordinate(device.release_date),
                    metric=metric,
                    value=value,
                    unit=UNITS[metric],
                    scope=metric_scope(metric, device.device_class),
                    value_kind=kind,
                    confidence=confidence,
                    source_tier=detail.get("source_tier", device.source_tier) if detail else device.source_tier,
                    references=references,
                    raw_benchmark_suite=str(detail.get("raw_benchmark_suite", "")) if detail else "",
                    raw_benchmark_value=str(detail.get("raw_benchmark_value", "")) if detail else "",
                    normalization=str(detail.get("normalization", "")) if detail else "",
                    precision=str(detail.get("precision", "")) if detail else "",
                    clock_basis=str(detail.get("clock_basis", "")) if detail else "",
                    calculation_operands=json.dumps(detail.get("calculation_operands", {}), sort_keys=True) if detail else "",
                    calculation_expression=str(detail.get("calculation_expression", "")) if detail else "",
                    validation_status=status,
                    notes=str(detail.get("notes", device.notes)) if detail else "No supported value found; retained as an explicit blank.",
                )
            )
    return result


def validate(devices: list[Device], observations: list[Observation], cutoff: str) -> None:
    if len({device.device_id for device in devices}) != len(devices):
        raise RuntimeError("device IDs are not unique")
    if len({obs.observation_id for obs in observations}) != len(observations):
        raise RuntimeError("observation IDs are not unique")
    if devices != sorted(devices, key=lambda item: (item.release_date, item.name, item.device_id)):
        raise RuntimeError("devices are not chronologically sorted")
    cutoff_date = date.fromisoformat(cutoff)
    allowed_kinds = {"direct", "measured", "calculated", "normalized", "unresolved"}
    allowed_confidence = {"direct", "high", "medium", "unresolved"}
    by_id = {device.device_id: device for device in devices}
    for device in devices:
        released = date.fromisoformat(device.release_date)
        if released > cutoff_date or device.shipping_status != "shipping":
            raise RuntimeError(f"{device.device_id} is not shipping by {cutoff}")
    for obs in observations:
        if obs.metric == "cpu_hardware_threads":
            raise RuntimeError("hardware-thread observations are not permitted")
        if obs.value_kind not in allowed_kinds or obs.confidence not in allowed_confidence:
            raise RuntimeError(f"invalid evidence classification for {obs.observation_id}")
        if obs.value is not None and (not obs.references or obs.value <= 0):
            raise RuntimeError(f"{obs.observation_id} lacks positive cited data")
        if obs.device_class == "gpu" and obs.metric == "cpu_single_thread_normalized" and obs.value is not None:
            raise RuntimeError(f"GPU received CPU single-thread data: {obs.observation_id}")
        if obs.metric in MULTICORE_METRICS and obs.value is not None:
            if (obs.device_class == "gpu"
                or obs.raw_benchmark_suite not in MULTICORE_SUITES
                or MULTICORE_SUITES[obs.raw_benchmark_suite][0] != obs.metric
                or float(obs.raw_benchmark_value) != obs.value):
                raise RuntimeError(f"invalid measured CPU throughput: {obs.observation_id}")
            if "one CPU package" not in obs.scope or obs.value_kind != "measured":
                raise RuntimeError(f"CPU throughput is not a one-chip measurement: {obs.observation_id}")
            if obs.validation_status == "eligible":
                raise RuntimeError("raw cross-suite scores must not be plotted")
        if obs.metric == THROUGHPUT_INDEX and obs.value is not None:
            if (obs.device_class == "gpu" or obs.raw_benchmark_suite not in MULTICORE_SUITES
                    or obs.value_kind != "normalized" or not obs.normalization
                    or not obs.calculation_operands or not obs.calculation_expression):
                raise RuntimeError(f"unsubstantiated throughput index: {obs.observation_id}")
            operands = json.loads(obs.calculation_operands)
            expected = operands["base_index"] * operands["raw_score"] / operands["denominator_for_suite"]
            raw_metric = MULTICORE_SUITES[obs.raw_benchmark_suite][0]
            if (not math.isclose(obs.value, expected, rel_tol=1e-12)
                    or operands["raw_score"] != float(by_id[obs.device_id].metrics[raw_metric]["value"])):
                raise RuntimeError(f"throughput index is inconsistent with raw source: {obs.observation_id}")
        if obs.metric == "cpu_physical_cores" and obs.validation_status == "eligible" and (
            obs.device_class not in {"cpu", "soc"} or obs.device_id in CORE_COUNT_EXCLUSIONS
        ):
            raise RuntimeError("noncomparable core count is plotted")
        if obs.metric == "fp32_dense_peak_gflops" and obs.value is not None:
            if obs.precision != "FP32 vector dense":
                raise RuntimeError(f"FP32 basis is unsafe for {obs.observation_id}")
            if obs.value_kind == "calculated" and (not obs.calculation_operands or not obs.calculation_expression):
                raise RuntimeError(f"calculated FP32 lacks operands for {obs.observation_id}")
        if obs.metric == "cpu_single_thread_normalized" and obs.value is not None:
            if obs.value_kind == "normalized" and not obs.normalization:
                raise RuntimeError(f"normalized benchmark lacks rule for {obs.observation_id}")
            if obs.raw_benchmark_suite == "Geekbench 7 single-core":
                operands = json.loads(obs.calculation_operands)
                expected = round(operands["anchor_index"] * operands["raw_score"] / operands["anchor_raw_score"], 1)
                if (obs.vendor != "Apple" or obs.value_kind != "normalized"
                        or not obs.raw_benchmark_value
                        or float(obs.raw_benchmark_value) != operands["raw_score"]
                        or obs.value != expected
                        or not any(url.startswith("https://browser.geekbench.com/") for url in obs.references)):
                    raise RuntimeError(f"Apple single-core bridge is inconsistent: {obs.observation_id}")
    cpu_like = {"cpu", "soc", "manycore"}
    missing_cores = [
        device.device_id for device in devices
        if device.device_class in cpu_like and "cpu_physical_cores" not in device.metrics
    ]
    if missing_cores:
        raise RuntimeError("CPU-class devices lack physical-core counts: " + ", ".join(missing_cores))
    core_points = [obs for obs in observations if obs.metric == "cpu_physical_cores" and obs.validation_status == "eligible"]
    if not core_points or not any(obs.value == 1 for obs in core_points) or not any(obs.value == 2 for obs in core_points):
        raise RuntimeError("core scatter must retain single- and multi-core devices")
    indexed = [obs for obs in observations if obs.metric == THROUGHPUT_INDEX and obs.validation_status == "eligible"]
    if len(indexed) < 30 or {obs.raw_benchmark_suite for obs in indexed} != set(MULTICORE_SUITES):
        raise RuntimeError("throughput index lost reviewed one-chip observations or a benchmark generation")


DEVICE_FIELDS = (
    "device_id", "device_name", "vendor", "device_class", "market", "package_kind",
    "release_date", "shipping_status", "source_tier", "reference_urls", "notes",
    *METRICS,
)
OBSERVATION_FIELDS = (
    "observation_id", "device_id", "device_name", "vendor", "market", "device_class", "component_type", "release_date",
    "release_year", "metric", "value", "unit", "scope", "value_kind", "confidence",
    "source_tier", "raw_benchmark_suite", "raw_benchmark_value", "normalization", "precision",
    "clock_basis", "calculation_operands", "calculation_expression", "validation_status",
    "reference_urls", "notes",
)


def write_csvs(directory: Path, devices: list[Device], observations: list[Observation]) -> tuple[Path, Path]:
    devices_path = directory / DEVICES_CSV.name
    observations_path = directory / OBSERVATIONS_CSV.name
    with devices_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=DEVICE_FIELDS, lineterminator="\n")
        writer.writeheader()
        for device in devices:
            row: dict[str, Any] = {
                "device_id": device.device_id,
                "device_name": device.name,
                "vendor": device.vendor,
                "device_class": device.device_class,
                "market": device.market,
                "package_kind": device.package_kind,
                "release_date": device.release_date,
                "shipping_status": device.shipping_status,
                "source_tier": device.source_tier,
                "reference_urls": "; ".join(device.references),
                "notes": device.notes,
            }
            for metric in METRICS:
                detail = device.metrics.get(metric)
                row[metric] = "" if not detail or detail.get("value") in (None, "") else detail["value"]
            writer.writerow(row)
    with observations_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=OBSERVATION_FIELDS, lineterminator="\n")
        writer.writeheader()
        for obs in observations:
            writer.writerow({
                "observation_id": obs.observation_id,
                "device_id": obs.device_id,
                "device_name": obs.device_name,
                "vendor": obs.vendor,
                "market": obs.market,
                "device_class": obs.device_class,
                "component_type": obs.component_type,
                "release_date": obs.release_date,
                "release_year": f"{obs.release_year:.6f}",
                "metric": obs.metric,
                "value": "" if obs.value is None else f"{obs.value:.12g}",
                "unit": obs.unit,
                "scope": obs.scope,
                "value_kind": obs.value_kind,
                "confidence": obs.confidence,
                "source_tier": obs.source_tier,
                "raw_benchmark_suite": obs.raw_benchmark_suite,
                "raw_benchmark_value": obs.raw_benchmark_value,
                "normalization": obs.normalization,
                "precision": obs.precision,
                "clock_basis": obs.clock_basis,
                "calculation_operands": obs.calculation_operands,
                "calculation_expression": obs.calculation_expression,
                "validation_status": obs.validation_status,
                "reference_urls": "; ".join(obs.references),
                "notes": obs.notes,
            })
    return devices_path, observations_path


def build_chart_data(devices: list[Device], observations: list[Observation], cutoff: str) -> dict[str, Any]:
    """A device is the hover unit; a calendar year is never a join key."""
    by_device: dict[str, dict[str, Observation]] = defaultdict(dict)
    for obs in observations:
        if obs.metric in by_device[obs.device_id]:
            raise RuntimeError(f"duplicate device metric: {obs.device_id} {obs.metric}")
        by_device[obs.device_id][obs.metric] = obs
    eligible = compute_chart.eligible(observations)
    plotted = compute_chart.visible_marker_observations(eligible)
    plotted_ids = {obs.observation_id for obs in plotted}
    guide_anchor_ids = {obs.observation_id for obs in eligible} - plotted_ids
    profiles: list[dict[str, Any]] = []
    for device in devices:
        device_observations = by_device[device.device_id]
        if set(device_observations) != set(METRICS):
            raise RuntimeError(f"incomplete observation ledger for {device.device_id}")
        x_year = release_coordinate(device.release_date)
        metrics: dict[str, Any] = {}
        points: list[dict[str, Any]] = []
        for metric in METRICS:
            obs = device_observations[metric]
            if obs.release_date != device.release_date or obs.release_year != x_year:
                raise RuntimeError(f"device/date mismatch for {obs.observation_id}")
            is_plotted = obs.observation_id in plotted_ids
            metrics[metric] = {
                "observation_id": obs.observation_id,
                "value": obs.value,
                "unit": obs.unit,
                "scope": obs.scope,
                "value_kind": obs.value_kind,
                "confidence": obs.confidence,
                "source_tier": obs.source_tier,
                "validation_status": obs.validation_status,
                "plotted": is_plotted,
                "reference_urls": list(obs.references),
                "raw_benchmark_suite": obs.raw_benchmark_suite or None,
                "raw_benchmark_value": obs.raw_benchmark_value or None,
                "normalization": obs.normalization or None,
                "clock_basis": obs.clock_basis or None,
                "calculation_operands": json.loads(obs.calculation_operands) if obs.calculation_operands else None,
                "calculation_expression": obs.calculation_expression or None,
                "notes": obs.notes,
            }
            if is_plotted:
                points.append({
                    "observation_id": obs.observation_id,
                    "metric": metric,
                    "series": compute_chart.series_key(obs),
                    "x_year": x_year,
                    "value": obs.value,
                    "display_value": compute_chart.display_value(obs),
                })
        profiles.append({
            "device_id": device.device_id,
            "name": device.name,
            "vendor": device.vendor,
            "device_class": device.device_class,
            "market": device.market,
            "release_date": device.release_date,
            "x_year": x_year,
            "plotted_points": points,
            "guide_anchor_ids": [
                device_observations[metric].observation_id
                for metric in METRICS
                if device_observations[metric].observation_id in guide_anchor_ids
            ],
            "metrics": metrics,
        })
    data = {"schema_version": 1, "shipping_cutoff": cutoff, "devices": profiles}
    verify_chart_data(data, devices, observations)
    return data


def verify_chart_data(data: dict[str, Any], devices: list[Device], observations: list[Observation]) -> None:
    profiles = data["devices"]
    if len(profiles) != len(devices) or {p["device_id"] for p in profiles} != {d.device_id for d in devices}:
        raise RuntimeError("chart profiles do not match the device roster")
    by_observation = {obs.observation_id: obs for obs in observations}
    expected = {obs.observation_id for obs in compute_chart.visible_marker_observations(compute_chart.eligible(observations))}
    seen: set[str] = set()
    seen_guides: set[str] = set()
    for profile in profiles:
        if set(profile["metrics"]) != set(METRICS):
            raise RuntimeError(f"missing metric fields for {profile['device_id']}")
        for metric, detail in profile["metrics"].items():
            obs = by_observation[detail["observation_id"]]
            if obs.device_id != profile["device_id"] or obs.metric != metric or obs.value != detail["value"]:
                raise RuntimeError(f"mixed device data in {detail['observation_id']}")
        for point in profile["plotted_points"]:
            point_id = point["observation_id"]
            obs = by_observation[point_id]
            if point_id in seen or obs.device_id != profile["device_id"] or point["x_year"] != profile["x_year"]:
                raise RuntimeError(f"duplicate or misaligned chart point {point_id}")
            if point["value"] != obs.value or point["metric"] != obs.metric or not profile["metrics"][obs.metric]["plotted"]:
                raise RuntimeError(f"chart point does not match profile {point_id}")
            seen.add(point_id)
        for point_id in profile["guide_anchor_ids"]:
            obs = by_observation[point_id]
            if point_id in seen_guides or obs.device_id != profile["device_id"]:
                raise RuntimeError(f"duplicate or misaligned guide anchor {point_id}")
            seen_guides.add(point_id)
    if seen != expected:
        raise RuntimeError(f"chart data point mismatch: missing={len(expected-seen)} extra={len(seen-expected)}")
    eligible_ids = {obs.observation_id for obs in compute_chart.eligible(observations)}
    if seen_guides != eligible_ids - expected:
        raise RuntimeError("chart guide anchors do not match the rendered chart")


PLOT_LEFT = 155
PLOT_RIGHT = 1755
PANEL_TOP = 150
PANEL_HEIGHT = 255
PANEL_GAP = 22
X_MIN = 1970.5
X_MAX = 2026.75


def panel_box(panel_index: int) -> tuple[float, float, float, float]:
    top = PANEL_TOP + panel_index * (PANEL_HEIGHT + PANEL_GAP)
    return PLOT_LEFT, top, PLOT_RIGHT, top + PANEL_HEIGHT


def map_x(year: float) -> float:
    return PLOT_LEFT + (year - X_MIN) / (X_MAX - X_MIN) * (PLOT_RIGHT - PLOT_LEFT)


def map_y(value: float, panel: Panel, panel_index: int) -> float:
    _, top, _, bottom = panel_box(panel_index)
    lo, hi = math.log10(panel.y_min), math.log10(panel.y_max)
    return bottom - (math.log10(value) - lo) / (hi - lo) * (bottom - top)


def panel_observations(observations: Iterable[Observation], panel_key: str) -> list[Observation]:
    return [obs for obs in observations if obs.value is not None and obs.panel == panel_key]


def series_order(panel: Panel, observations: list[Observation]) -> list[str]:
    present = {obs.series_key for obs in observations}
    preferred = ["cpu", "soc", "integrated-gpu", "discrete-gpu", "manycore", "cpu-cores", "gpu-units", "single-thread"]
    return [key for key in preferred if key in present]


def marker_path(shape: str, x: float, y: float, radius: float = 6.5) -> str:
    if shape == "square":
        return f"M {x-radius:.2f},{y-radius:.2f} h {2*radius:.2f} v {2*radius:.2f} h {-2*radius:.2f} Z"
    if shape == "triangle":
        return f"M {x:.2f},{y-radius:.2f} L {x+radius:.2f},{y+radius:.2f} L {x-radius:.2f},{y+radius:.2f} Z"
    if shape == "diamond":
        return f"M {x:.2f},{y-radius:.2f} L {x+radius:.2f},{y:.2f} L {x:.2f},{y+radius:.2f} L {x-radius:.2f},{y:.2f} Z"
    return ""


def shape_for(obs: Observation) -> str:
    return {"soc": "square", "gpu": "triangle", "manycore": "diamond"}.get(obs.device_class, "circle")


def svg_element(tag: str, attributes: dict[str, str] | None = None, text: str | None = None) -> ET.Element:
    element = ET.Element(f"{{{SVG_NS}}}{tag}", attributes or {})
    element.text = text
    return element


def add_svg_marker(parent: ET.Element, obs: Observation, x: float, y: float) -> None:
    colour = SERIES_COLOURS[obs.series_key]
    point = svg_element("g", {
        "id": obs.observation_id,
        "class": "data-point",
        "data-device-id": obs.device_id,
        "data-series": obs.series_key,
        "data-metric": obs.metric,
        "data-year": f"{obs.release_year:.6f}",
        "data-value": f"{obs.value:.12g}",
        "data-confidence": obs.confidence,
        "data-value-kind": obs.value_kind,
    })
    point.append(svg_element("title", text=f"{obs.device_name}: {obs.value:g} {obs.unit} ({obs.value_kind}, {obs.confidence})"))
    style = {
        "stroke": colour,
        "stroke-width": "2.2",
        "fill": colour if obs.filled else "white",
        "fill-opacity": f"{POINT_OPACITY:.2f}" if obs.filled else "1",
        "stroke-opacity": f"{POINT_OPACITY:.2f}",
    }
    shape = shape_for(obs)
    if shape == "circle":
        point.append(svg_element("circle", {**style, "cx": f"{x:.2f}", "cy": f"{y:.2f}", "r": "6.5"}))
    else:
        point.append(svg_element("path", {**style, "d": marker_path(shape, x, y)}))
    parent.append(point)


def render_svg(path: Path, observations: list[Observation], cutoff: str) -> None:
    ET.register_namespace("", SVG_NS)
    root = svg_element("svg", {"width": str(WIDTH), "height": str(HEIGHT), "viewBox": f"0 0 {WIDTH} {HEIGHT}", "version": "1.1"})
    root.append(svg_element("title", text="55 Years of CPU and GPU Compute Trends"))
    root.append(svg_element("desc", text="Six aligned logarithmic panels with independently sourced CPU and GPU observations."))
    figure = svg_element("g", {"id": "figure", "class": "compute-trends-figure"})
    figure.append(svg_element("rect", {"id": "figure-background", "x": "0", "y": "0", "width": str(WIDTH), "height": str(HEIGHT), "fill": "white"}))
    title_group = svg_element("g", {"id": "figure-title", "class": "figure-title"})
    title_group.append(svg_element("text", {"x": "155", "y": "70", "font-size": "32", "font-weight": "bold"}, "55 Years of CPU and GPU Compute Trends"))
    title_group.append(svg_element("text", {"x": "155", "y": "105", "font-size": "17", "fill": "#444"}, "1971-2026; shared time axis, independent metric definitions"))
    figure.append(title_group)
    panels_group = svg_element("g", {"id": "panels", "class": "chart-panels"})
    x_ticks = [1971, 1980, 1990, 2000, 2010, 2020, 2026]
    for index, panel in enumerate(PANELS):
        left, top, right, bottom = panel_box(index)
        panel_group = svg_element("g", {"id": f"panel-{panel.key}", "class": "chart-panel", "data-panel": panel.key})
        panel_group.append(svg_element("title", text=panel.title))
        panel_group.append(svg_element("rect", {"id": f"panel-{panel.key}-background", "x": str(left), "y": str(top), "width": str(right-left), "height": str(bottom-top), "fill": "#FFFFFF"}))
        grid = svg_element("g", {"id": f"panel-{panel.key}-grid", "class": "panel-grid"})
        exp_min = math.ceil(math.log10(panel.y_min))
        exp_max = math.floor(math.log10(panel.y_max))
        for exponent in range(exp_min, exp_max + 1):
            value = 10.0 ** exponent
            y = map_y(value, panel, index)
            grid.append(svg_element("line", {"id": f"panel-{panel.key}-grid-y-{exponent}", "x1": str(left), "x2": str(right), "y1": f"{y:.2f}", "y2": f"{y:.2f}", "stroke": "#D6DADF", "stroke-width": "1"}))
        for tick in x_ticks:
            x = map_x(tick)
            grid.append(svg_element("line", {"id": f"panel-{panel.key}-grid-x-{tick}", "x1": f"{x:.2f}", "x2": f"{x:.2f}", "y1": str(top), "y2": str(bottom), "stroke": "#E4E7EA", "stroke-width": "1"}))
        panel_group.append(grid)
        axes = svg_element("g", {"id": f"panel-{panel.key}-axes", "class": "panel-axes"})
        axes.append(svg_element("rect", {"id": f"panel-{panel.key}-frame", "x": str(left), "y": str(top), "width": str(right-left), "height": str(bottom-top), "fill": "none", "stroke": "#333", "stroke-width": "1.5"}))
        y_ticks_group = svg_element("g", {"id": f"panel-{panel.key}-y-ticks", "class": "axis-ticks"})
        for exponent in range(exp_min, exp_max + 1):
            y = map_y(10.0 ** exponent, panel, index)
            y_ticks_group.append(svg_element("text", {"id": f"panel-{panel.key}-y-label-{exponent}", "x": str(left-16), "y": f"{y+6:.2f}", "text-anchor": "end", "font-size": "16", "fill": "#333"}, f"10^{exponent}"))
        axes.append(y_ticks_group)
        if index == len(PANELS) - 1:
            x_ticks_group = svg_element("g", {"id": "shared-x-axis", "class": "axis-ticks"})
            for tick in x_ticks:
                x = map_x(tick)
                x_ticks_group.append(svg_element("text", {"id": f"x-label-{tick}", "x": f"{x:.2f}", "y": f"{bottom+32:.2f}", "text-anchor": "middle", "font-size": "16", "fill": "#333"}, str(tick)))
            x_ticks_group.append(svg_element("text", {"id": "shared-x-axis-title", "x": f"{(left+right)/2:.2f}", "y": f"{bottom+62:.2f}", "text-anchor": "middle", "font-size": "18"}, "Year available"))
            axes.append(x_ticks_group)
        panel_group.append(axes)
        labels = svg_element("g", {"id": f"panel-{panel.key}-labels", "class": "panel-labels"})
        labels.append(svg_element("text", {"id": f"panel-{panel.key}-title", "x": str(left+12), "y": str(top+27), "font-size": "20", "font-weight": "bold"}, panel.title))
        labels.append(svg_element("text", {"id": f"panel-{panel.key}-unit", "x": str(left+430), "y": str(top+25), "font-size": "15", "text-anchor": "start", "fill": "#555"}, panel.unit_label))
        panel_group.append(labels)
        eligible = panel_observations(observations, panel.key)
        data = svg_element("g", {"id": f"panel-{panel.key}-series", "class": "panel-series"})
        for series_key in series_order(panel, eligible):
            series_group = svg_element("g", {"id": f"series-{panel.key}-{series_key}", "class": "data-series", "data-series": series_key})
            series_group.append(svg_element("title", text=SERIES_LABELS[series_key]))
            for obs in [item for item in eligible if item.series_key == series_key]:
                device_group = svg_element("g", {"id": f"device-{panel.key}-{series_key}-{obs.device_id}", "class": "device", "data-device-id": obs.device_id})
                add_svg_marker(device_group, obs, map_x(obs.release_year), map_y(obs.value or 1, panel, index))
                series_group.append(device_group)
            data.append(series_group)
        panel_group.append(data)
        panels_group.append(panel_group)
    figure.append(panels_group)
    legend = svg_element("g", {"id": "figure-legend", "class": "chart-legend"})
    legend.append(svg_element("text", {"x": "155", "y": "1895", "font-size": "15"}, "Series: CPU = blue circle; SoC = teal square; discrete GPU = orange triangle; many-core / APU = magenta diamond."))
    legend.append(svg_element("text", {"x": "155", "y": "1925", "font-size": "15"}, "Fill: GPU or GPU component; outline: CPU or other. Evidence quality is recorded in the CSV."))
    figure.append(legend)
    notes = svg_element("g", {"id": "figure-notes", "class": "figure-notes"})
    notes.append(svg_element("text", {"x": "155", "y": "1965", "font-size": "14", "fill": "#444"}, "FP32 is dense vector peak capacity. Equal GFLOP/s does not imply equal application performance. CPU cores exclude SMT/Hyper-Threading."))
    notes.append(svg_element("text", {"x": "155", "y": "1995", "font-size": "13", "fill": "#666"}, f"Shipping products and cited metrics available by {cutoff}. Missing values are not plotted."))
    figure.append(notes)
    root.append(figure)
    for element in root.iter():
        if element.tag.endswith("text"):
            element.set("font-family", FONT_STACK)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def font_path(*, bold: bool = False) -> Path:
    candidate = Path(reportlab.__file__).resolve().parent / "fonts" / ("VeraBd.ttf" if bold else "Vera.ttf")
    if not candidate.is_file():
        raise RuntimeError("bundled Bitstream Vera font is missing")
    return candidate


def draw_marker(draw: Any, obs: Observation, x: float, y: float, colour: str, *, pdf: bool = False) -> None:
    radius = 6.5
    fill = colour if obs.filled else "white"
    shape = shape_for(obs)
    if pdf:
        c: canvas.Canvas = draw
        c.setStrokeColor(colour)
        c.setLineWidth(2.2)
        if obs.filled:
            c.setFillColor(colour)
            c.setFillAlpha(POINT_OPACITY)
        else:
            c.setFillColorRGB(1, 1, 1)
            c.setFillAlpha(1)
        c.setStrokeAlpha(POINT_OPACITY)
        yy = HEIGHT - y
        path = c.beginPath()
        if shape == "circle":
            c.circle(x, yy, radius, stroke=1, fill=1)
        else:
            coordinates = {
                "square": [(x-radius, yy-radius), (x+radius, yy-radius), (x+radius, yy+radius), (x-radius, yy+radius)],
                "triangle": [(x, yy+radius), (x+radius, yy-radius), (x-radius, yy-radius)],
                "diamond": [(x, yy+radius), (x+radius, yy), (x, yy-radius), (x-radius, yy)],
            }[shape]
            path.moveTo(*coordinates[0])
            for point in coordinates[1:]:
                path.lineTo(*point)
            path.close()
            c.drawPath(path, stroke=1, fill=1)
        c.setFillAlpha(1)
        c.setStrokeAlpha(1)
        return
    image_draw: ImageDraw.ImageDraw = draw
    rgba = tuple(int(colour[index:index+2], 16) for index in (1, 3, 5)) + (int(255 * POINT_OPACITY),)
    outline = rgba
    interior = rgba if obs.filled else (255, 255, 255, 255)
    box = (x-radius, y-radius, x+radius, y+radius)
    if shape == "circle":
        image_draw.ellipse(box, fill=interior, outline=outline, width=2)
    elif shape == "square":
        image_draw.rectangle(box, fill=interior, outline=outline, width=2)
    elif shape == "triangle":
        image_draw.polygon([(x, y-radius), (x+radius, y+radius), (x-radius, y+radius)], fill=interior, outline=outline)
    else:
        image_draw.polygon([(x, y-radius), (x+radius, y), (x, y+radius), (x-radius, y)], fill=interior, outline=outline)


def render_png(path: Path, observations: list[Observation], cutoff: str) -> None:
    image = Image.new("RGBA", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image, "RGBA")
    regular = ImageFont.truetype(str(font_path()), 16)
    small = ImageFont.truetype(str(font_path()), 14)
    title_font = ImageFont.truetype(str(font_path(bold=True)), 32)
    panel_font = ImageFont.truetype(str(font_path(bold=True)), 20)
    draw.text((155, 42), "55 Years of CPU and GPU Compute Trends", font=title_font, fill="#111111")
    draw.text((155, 88), "1971-2026; shared time axis, independent metric definitions", font=regular, fill="#444444")
    x_ticks = [1971, 1980, 1990, 2000, 2010, 2020, 2026]
    for index, panel in enumerate(PANELS):
        left, top, right, bottom = panel_box(index)
        exp_min = math.ceil(math.log10(panel.y_min))
        exp_max = math.floor(math.log10(panel.y_max))
        for exponent in range(exp_min, exp_max + 1):
            y = map_y(10.0 ** exponent, panel, index)
            draw.line((left, y, right, y), fill="#D6DADF", width=1)
            label = f"10^{exponent}"
            bbox = draw.textbbox((0, 0), label, font=regular)
            draw.text((left - 16 - (bbox[2]-bbox[0]), y-8), label, font=regular, fill="#333333")
        for tick in x_ticks:
            x = map_x(tick)
            draw.line((x, top, x, bottom), fill="#E4E7EA", width=1)
        draw.rectangle((left, top, right, bottom), outline="#333333", width=2)
        draw.text((left+12, top+7), panel.title, font=panel_font, fill="#111111")
        draw.text((left+430, top+9), panel.unit_label, font=small, fill="#555555")
        for obs in panel_observations(observations, panel.key):
            draw_marker(draw, obs, map_x(obs.release_year), map_y(obs.value or 1, panel, index), SERIES_COLOURS[obs.series_key])
        if index == len(PANELS) - 1:
            for tick in x_ticks:
                label = str(tick)
                bbox = draw.textbbox((0, 0), label, font=regular)
                draw.text((map_x(tick)-(bbox[2]-bbox[0])/2, bottom+12), label, font=regular, fill="#333333")
            draw.text(((left+right)/2-55, bottom+43), "Year available", font=regular, fill="#111111")
    draw.text((155, 1880), "Series: CPU = blue circle; SoC = teal square; discrete GPU = orange triangle; many-core / APU = magenta diamond.", font=small, fill="#222222")
    draw.text((155, 1910), "Fill: GPU or GPU component; outline: CPU or other. Evidence quality is recorded in the CSV.", font=small, fill="#222222")
    draw.text((155, 1950), "FP32 is dense vector peak capacity. Equal GFLOP/s does not imply equal application performance. CPU cores exclude SMT/Hyper-Threading.", font=small, fill="#444444")
    draw.text((155, 1980), f"Shipping products and cited metrics available by {cutoff}. Missing values are not plotted.", font=small, fill="#666666")
    image.convert("RGB").save(path, format="PNG", optimize=True)


def pdf_text(c: canvas.Canvas, x: float, y: float, text: str, size: float, colour: str = "#111111", *, bold: bool = False) -> None:
    c.setFont("VeraBold" if bold else "Vera", size)
    c.setFillColor(colour)
    c.drawString(x, HEIGHT-y, text)


def render_pdf_raw(path: Path, observations: list[Observation], cutoff: str) -> tuple[list[str], list[str]]:
    pdfmetrics.registerFont(TTFont("Vera", str(font_path())))
    pdfmetrics.registerFont(TTFont("VeraBold", str(font_path(bold=True))))
    c = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT), pageCompression=0)
    c.setTitle("55 Years of CPU and GPU Compute Trends")
    c.setAuthor("microprocessor-trend-data")
    pdf_text(c, 155, 70, "55 Years of CPU and GPU Compute Trends", 32, bold=True)
    pdf_text(c, 155, 105, "1971-2026; shared time axis, independent metric definitions", 17, "#444444")
    x_ticks = [1971, 1980, 1990, 2000, 2010, 2020, 2026]
    point_ids: list[str] = []
    series_ids: list[str] = []
    mcid = 0
    for index, panel in enumerate(PANELS):
        c._code.append(f"%OC_BEGIN panel-{panel.key}")
        left, top, right, bottom = panel_box(index)
        exp_min = math.ceil(math.log10(panel.y_min))
        exp_max = math.floor(math.log10(panel.y_max))
        c.setLineWidth(1)
        for exponent in range(exp_min, exp_max + 1):
            y = map_y(10.0 ** exponent, panel, index)
            c.setStrokeColor("#D6DADF")
            c.line(left, HEIGHT-y, right, HEIGHT-y)
            pdf_text(c, left-62, y+6, f"10^{exponent}", 16, "#333333")
        for tick in x_ticks:
            x = map_x(tick)
            c.setStrokeColor("#E4E7EA")
            c.line(x, HEIGHT-top, x, HEIGHT-bottom)
        c.setStrokeColor("#333333")
        c.setLineWidth(1.5)
        c.rect(left, HEIGHT-bottom, right-left, bottom-top, stroke=1, fill=0)
        pdf_text(c, left+12, top+27, panel.title, 20, bold=True)
        pdf_text(c, left+430, top+25, panel.unit_label, 15, "#555555")
        if index == len(PANELS) - 1:
            for tick in x_ticks:
                pdf_text(c, map_x(tick)-17, bottom+31, str(tick), 16, "#333333")
            pdf_text(c, (left+right)/2-55, bottom+61, "Year available", 18)
        eligible = panel_observations(observations, panel.key)
        for series_key in series_order(panel, eligible):
            series_id = f"series-{panel.key}-{series_key}"
            series_ids.append(series_id)
            c._code.append(f"%OC_BEGIN {series_id}")
            for obs in [item for item in eligible if item.series_key == series_key]:
                safe_id = obs.observation_id.replace("(", "-").replace(")", "-")
                c._code.append(f"/Figure << /MCID {mcid} /ID ({safe_id}) >> BDC")
                draw_marker(c, obs, map_x(obs.release_year), map_y(obs.value or 1, panel, index), SERIES_COLOURS[obs.series_key], pdf=True)
                c._code.append("EMC")
                point_ids.append(obs.observation_id)
                mcid += 1
            c._code.append("%OC_END")
        c._code.append("%OC_END")
    pdf_text(c, 155, 1895, "Series: CPU = blue circle; SoC = teal square; discrete GPU = orange triangle; many-core / APU = magenta diamond.", 14)
    pdf_text(c, 155, 1925, "Fill: GPU or GPU component; outline: CPU or other. Evidence quality is recorded in the CSV.", 14)
    pdf_text(c, 155, 1965, "FP32 is dense vector peak capacity. Equal GFLOP/s does not imply equal application performance. CPU cores exclude SMT/Hyper-Threading.", 14, "#444444")
    pdf_text(c, 155, 1995, f"Shipping products and cited metrics available by {cutoff}. Missing values are not plotted.", 13, "#666666")
    c.showPage()
    c.save()
    return point_ids, list(dict.fromkeys(series_ids))


def structure_pdf(raw_path: Path, output_path: Path, observations: list[Observation]) -> None:
    reader = PdfReader(raw_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.pdf_header = "%PDF-1.7"
    page = writer.pages[0]
    content_data = page.get_contents().get_data().decode("latin-1")
    content_data = re.sub(r"BT\s+/\S+\s+[0-9.]+\s+Tf\s+[0-9.]+\s+TL\s+ET\s*", "", content_data)
    resources = page[NameObject("/Resources")].get_object()
    font_resources = resources[NameObject("/Font")].get_object()
    for resource_name in list(font_resources):
        if not re.search(rf"{re.escape(str(resource_name))}\s+[0-9.]+\s+Tf\b", content_data):
            del font_resources[resource_name]
    properties = DictionaryObject()
    resources[NameObject("/Properties")] = properties
    ocg_refs: dict[str, Any] = {}
    order = ArrayObject()
    eligible_by_panel = {panel.key: panel_observations(observations, panel.key) for panel in PANELS}
    for panel in PANELS:
        panel_id = f"panel-{panel.key}"
        panel_ref = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/OCG"), NameObject("/Name"): TextStringObject(panel.title)}))
        ocg_refs[panel_id] = panel_ref
        children = ArrayObject([TextStringObject(panel.title), panel_ref])
        for series_key in series_order(panel, eligible_by_panel[panel.key]):
            series_id = f"series-{panel.key}-{series_key}"
            ref = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/OCG"), NameObject("/Name"): TextStringObject(f"{panel.title}: {SERIES_LABELS[series_key]}")}))
            ocg_refs[series_id] = ref
            children.append(ref)
        order.append(children)
    for index, (identifier, ref) in enumerate(ocg_refs.items(), 1):
        property_name = NameObject(f"/Layer{index}")
        properties[property_name] = ref
        content_data = content_data.replace(f"%OC_BEGIN {identifier}\n", f"/OC {property_name} BDC\n")
    content_data = content_data.replace("%OC_END\n", "EMC\n")
    if "%OC_" in content_data:
        raise RuntimeError("unresolved PDF layer marker")
    stream = DecodedStreamObject()
    stream.set_data(content_data.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    all_ocgs = ArrayObject(list(ocg_refs.values()))
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): all_ocgs,
        NameObject("/D"): DictionaryObject({NameObject("/Order"): order, NameObject("/ON"): all_ocgs}),
    })
    point_refs: list[Any] = []
    panel_struct_refs: list[Any] = []
    mcid = 0
    for panel in PANELS:
        series_struct_refs: list[Any] = []
        eligible = eligible_by_panel[panel.key]
        for series_key in series_order(panel, eligible):
            figures: list[Any] = []
            for obs in [item for item in eligible if item.series_key == series_key]:
                figure = DictionaryObject({
                    NameObject("/Type"): NameObject("/StructElem"),
                    NameObject("/S"): NameObject("/Figure"),
                    NameObject("/T"): TextStringObject(obs.observation_id),
                    NameObject("/Alt"): TextStringObject(f"{obs.device_name}: {obs.value:g} {obs.unit}; {obs.value_kind}, {obs.confidence}"),
                    NameObject("/Pg"): page.indirect_reference,
                    NameObject("/K"): NumberObject(mcid),
                })
                ref = writer._add_object(figure)
                figures.append(ref)
                point_refs.append(ref)
                mcid += 1
            section = DictionaryObject({NameObject("/Type"): NameObject("/StructElem"), NameObject("/S"): NameObject("/Sect"), NameObject("/T"): TextStringObject(SERIES_LABELS[series_key]), NameObject("/K"): ArrayObject(figures)})
            section_ref = writer._add_object(section)
            for figure_ref in figures:
                figure_ref.get_object()[NameObject("/P")] = section_ref
            series_struct_refs.append(section_ref)
        panel_section = DictionaryObject({NameObject("/Type"): NameObject("/StructElem"), NameObject("/S"): NameObject("/Sect"), NameObject("/T"): TextStringObject(panel.title), NameObject("/K"): ArrayObject(series_struct_refs)})
        panel_ref = writer._add_object(panel_section)
        for series_ref in series_struct_refs:
            series_ref.get_object()[NameObject("/P")] = panel_ref
        panel_struct_refs.append(panel_ref)
    parent_tree = writer._add_object(DictionaryObject({NameObject("/Nums"): ArrayObject([NumberObject(0), ArrayObject(point_refs)])}))
    struct_root = DictionaryObject({NameObject("/Type"): NameObject("/StructTreeRoot"), NameObject("/K"): ArrayObject(panel_struct_refs), NameObject("/ParentTree"): parent_tree, NameObject("/ParentTreeNextKey"): NumberObject(1)})
    struct_root_ref = writer._add_object(struct_root)
    for panel_ref in panel_struct_refs:
        panel_ref.get_object()[NameObject("/P")] = struct_root_ref
    page[NameObject("/StructParents")] = NumberObject(0)
    writer._root_object[NameObject("/StructTreeRoot")] = struct_root_ref
    writer._root_object[NameObject("/MarkInfo")] = DictionaryObject({NameObject("/Marked"): BooleanObject(True)})
    writer._root_object[NameObject("/Lang")] = TextStringObject("en-GB")
    with output_path.open("wb") as stream_out:
        writer.write(stream_out)


def ps_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def ps_colour(hex_colour: str, lighten: bool = False) -> str:
    rgb = [int(hex_colour[index:index+2], 16) / 255 for index in (1, 3, 5)]
    if lighten:
        rgb = [1 - (1 - value) * POINT_OPACITY for value in rgb]
    return " ".join(f"{value:.4f}" for value in rgb)


def eps_marker(obs: Observation, x: float, y: float) -> list[str]:
    yy = HEIGHT - y
    radius = 6.5
    colour = SERIES_COLOURS[obs.series_key]
    lines = ["gsave", f"{ps_colour(colour, obs.filled)} setrgbcolor", "2.2 setlinewidth", "newpath"]
    shape = shape_for(obs)
    if shape == "circle":
        lines.append(f"{x:.2f} {yy:.2f} {radius:.2f} 0 360 arc closepath")
    else:
        coords = {
            "square": [(x-radius, yy-radius), (x+radius, yy-radius), (x+radius, yy+radius), (x-radius, yy+radius)],
            "triangle": [(x, yy+radius), (x+radius, yy-radius), (x-radius, yy-radius)],
            "diamond": [(x, yy+radius), (x+radius, yy), (x, yy-radius), (x-radius, yy)],
        }[shape]
        lines.append(f"{coords[0][0]:.2f} {coords[0][1]:.2f} moveto")
        lines.extend(f"{px:.2f} {py:.2f} lineto" for px, py in coords[1:])
        lines.append("closepath")
    if obs.filled:
        lines.extend(["gsave fill grestore", f"{ps_colour(colour)} setrgbcolor", "stroke"])
    else:
        lines.extend(["1 1 1 setrgbcolor gsave fill grestore", f"{ps_colour(colour)} setrgbcolor", "stroke"])
    lines.append("grestore")
    return lines


def render_eps(path: Path, observations: list[Observation], cutoff: str) -> None:
    lines = [
        "%!PS-Adobe-3.0 EPSF-3.0", f"%%BoundingBox: 0 0 {WIDTH} {HEIGHT}",
        "%%Title: 55 Years of CPU and GPU Compute Trends", "%%Creator: generate_compute_trends.py",
        "%%DocumentFonts: Helvetica Helvetica-Bold", "%%Pages: 1", "%%EndComments", "%%BeginObject: figure", "1 1 1 setrgbcolor", f"0 0 {WIDTH} {HEIGHT} rectfill",
        "/Helvetica-Bold findfont 32 scalefont setfont", "0 0 0 setrgbcolor", f"155 {HEIGHT-70} moveto (55 Years of CPU and GPU Compute Trends) show",
        "/Helvetica findfont 17 scalefont setfont", f"155 {HEIGHT-105} moveto (1971-2026; shared time axis, independent metric definitions) show",
        "%%BeginObject: panels",
    ]
    x_ticks = [1971, 1980, 1990, 2000, 2010, 2020, 2026]
    for index, panel in enumerate(PANELS):
        left, top, right, bottom = panel_box(index)
        lines.extend([f"%%BeginObject: panel-{panel.key}", f"%%BeginObject: panel-{panel.key}-grid"])
        exp_min = math.ceil(math.log10(panel.y_min))
        exp_max = math.floor(math.log10(panel.y_max))
        for exponent in range(exp_min, exp_max + 1):
            y = HEIGHT - map_y(10.0 ** exponent, panel, index)
            lines.extend(["0.84 0.86 0.88 setrgbcolor", "1 setlinewidth", f"newpath {left} {y:.2f} moveto {right} {y:.2f} lineto stroke"])
        for tick in x_ticks:
            x = map_x(tick)
            lines.extend(["0.89 0.91 0.92 setrgbcolor", f"newpath {x:.2f} {HEIGHT-top} moveto {x:.2f} {HEIGHT-bottom} lineto stroke"])
        lines.extend([
            "%%EndObject", f"%%BeginObject: panel-{panel.key}-axes",
            "0.2 0.2 0.2 setrgbcolor", "1.5 setlinewidth",
            f"newpath {left} {HEIGHT-bottom} moveto {right} {HEIGHT-bottom} lineto {right} {HEIGHT-top} lineto {left} {HEIGHT-top} lineto closepath stroke",
            "/Helvetica findfont 16 scalefont setfont",
        ])
        for exponent in range(exp_min, exp_max + 1):
            y = HEIGHT - map_y(10.0 ** exponent, panel, index)
            lines.append(f"{left-62} {y-6:.2f} moveto (10^{exponent}) show")
        if index == len(PANELS) - 1:
            for tick in x_ticks:
                lines.append(f"{map_x(tick)-17:.2f} {HEIGHT-bottom-31:.2f} moveto ({tick}) show")
            lines.extend([
                "/Helvetica findfont 18 scalefont setfont",
                f"{(left+right)/2-55:.2f} {HEIGHT-bottom-61:.2f} moveto (Year available) show",
            ])
        lines.extend([
            "%%EndObject", f"%%BeginObject: panel-{panel.key}-labels",
            "/Helvetica-Bold findfont 20 scalefont setfont", "0 0 0 setrgbcolor",
            f"{left+12} {HEIGHT-top-27} moveto ({ps_escape(panel.title)}) show",
            "/Helvetica findfont 15 scalefont setfont", "0.33 0.33 0.33 setrgbcolor",
            f"{left+430} {HEIGHT-top-25} moveto ({ps_escape(panel.unit_label)}) show",
            "%%EndObject", f"%%BeginObject: panel-{panel.key}-series",
        ])
        eligible = panel_observations(observations, panel.key)
        for series_key in series_order(panel, eligible):
            lines.append(f"%%BeginObject: series-{panel.key}-{series_key}")
            for obs in [item for item in eligible if item.series_key == series_key]:
                lines.extend([
                    f"%%BeginObject: device-{panel.key}-{series_key}-{obs.device_id}",
                    f"%%BeginObject: {obs.observation_id}",
                    f"%%PointData: device={obs.device_id} metric={obs.metric} year={obs.release_year:.6f} value={obs.value:.12g} confidence={obs.confidence}",
                    *eps_marker(obs, map_x(obs.release_year), map_y(obs.value or 1, panel, index)),
                    "%%EndObject", "%%EndObject",
                ])
            lines.append("%%EndObject")
        lines.extend(["%%EndObject", "%%EndObject"])
    lines.extend([
        "%%EndObject", "%%BeginObject: figure-legend", "/Helvetica findfont 14 scalefont setfont", "0.1 0.1 0.1 setrgbcolor",
        f"155 {HEIGHT-1895} moveto (Series: CPU = blue circle; SoC = teal square; discrete GPU = orange triangle; many-core / APU = magenta diamond.) show",
        f"155 {HEIGHT-1925} moveto (Fill: GPU or GPU component; outline: CPU or other. Evidence quality is recorded in the CSV.) show", "%%EndObject",
        "%%BeginObject: figure-notes", "/Helvetica findfont 14 scalefont setfont", "0.25 0.25 0.25 setrgbcolor",
        f"155 {HEIGHT-1965} moveto (FP32 is dense vector peak capacity. Equal GFLOP/s does not imply equal application performance. CPU cores exclude SMT/Hyper-Threading.) show",
        f"155 {HEIGHT-1995} moveto (Shipping products and cited metrics available by {ps_escape(cutoff)}. Missing values are not plotted.) show",
        "%%EndObject", "%%EndObject", "showpage", "%%EOF",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="latin-1")


def verify_svg(path: Path, eligible: list[Observation]) -> None:
    root = ET.parse(path).getroot()
    if any(element.tag == f"{{{SVG_NS}}}image" for element in root.iter()):
        raise RuntimeError("SVG contains a raster image")
    groups = [element for element in root.iter(f"{{{SVG_NS}}}g")]
    ids = [group.get("id", "") for group in groups]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("SVG has anonymous or duplicate group IDs")
    point_ids = [element.get("id") for element in groups if element.get("class") == "data-point"]
    expected = [obs.observation_id for obs in eligible]
    if sorted(point_ids) != sorted(expected):
        raise RuntimeError("SVG point objects do not match eligible observations")
    for panel in PANELS:
        for suffix in ("grid", "axes", "labels", "series"):
            if root.find(f".//*[@id='panel-{panel.key}-{suffix}']") is None:
                raise RuntimeError(f"SVG missing panel-{panel.key}-{suffix}")
    serialized = path.read_text(encoding="utf-8").lower()
    if any(token in serialized for token in ("@font-face", "data:font", "<font")):
        raise RuntimeError("SVG embeds a font")


def count_pdf_images(reader: PdfReader) -> int:
    count = 0
    for page in reader.pages:
        resources = page.get("/Resources", {}).get_object()
        xobjects = resources.get("/XObject", {})
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        for ref in xobjects.values():
            if str(ref.get_object().get("/Subtype")) == "/Image":
                count += 1
    return count


def verify_pdf(path: Path, eligible: list[Observation]) -> None:
    reader = PdfReader(path)
    if len(reader.pages) != 1 or count_pdf_images(reader):
        raise RuntimeError("PDF must be one vector-only page")
    root = reader.trailer["/Root"]
    if root.get("/OCProperties") is None or root.get("/StructTreeRoot") is None:
        raise RuntimeError("PDF layers or tagged structure are missing")
    content = reader.pages[0].get_contents().get_data().decode("latin-1")
    if content.count("/Figure << /MCID") != len(eligible):
        raise RuntimeError("PDF marked point count mismatch")
    if len(root["/OCProperties"].get_object()["/OCGs"]) < len(PANELS):
        raise RuntimeError("PDF panel/series layers are incomplete")
    fonts = reader.pages[0]["/Resources"]["/Font"].get_object()
    if not fonts:
        raise RuntimeError("PDF has no font resources")
    for font_ref in fonts.values():
        font = font_ref.get_object()
        descriptor = font.get("/FontDescriptor")
        if descriptor is None and font.get("/DescendantFonts"):
            descriptor = font["/DescendantFonts"][0].get_object().get("/FontDescriptor")
        descriptor = descriptor.get_object() if hasattr(descriptor, "get_object") else descriptor
        if descriptor is None or not any(descriptor.get(name) for name in ("/FontFile", "/FontFile2", "/FontFile3")):
            raise RuntimeError("PDF font is not embedded")


def verify_eps(path: Path, eligible: list[Observation]) -> None:
    text = path.read_text(encoding="latin-1")
    executable = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("%"))
    if re.search(r"\b(?:image|colorimage|imagemask)\b", executable, re.IGNORECASE):
        raise RuntimeError("EPS contains a raster operator")
    if text.count("%%BeginObject:") != text.count("%%EndObject"):
        raise RuntimeError("EPS object groups are unbalanced")
    if text.count("%%PointData:") != len(eligible):
        raise RuntimeError("EPS point count mismatch")
    for obs in eligible:
        if text.count(f"%%BeginObject: {obs.observation_id}\n") != 1:
            raise RuntimeError(f"EPS missing {obs.observation_id}")


def verify_png(path: Path) -> None:
    with Image.open(path) as image:
        if image.size != (WIDTH, HEIGHT) or image.format != "PNG":
            raise RuntimeError("PNG dimensions or format are incorrect")


def install_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def generate(formats: Iterable[str]) -> None:
    devices, cutoff = load_devices()
    observations = build_observations(devices)
    validate(devices, observations, cutoff)
    eligible = compute_chart.eligible(observations)
    chart_data = build_chart_data(devices, observations, cutoff)
    with tempfile.TemporaryDirectory(prefix="compute-trends-", dir=ROOT) as temp_name:
        temp = Path(temp_name)
        devices_csv, observations_csv = write_csvs(temp, devices, observations)
        chart_data_path = temp / CHART_DATA_JSON.name
        chart_data_path.write_text(json.dumps(chart_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        verify_chart_data(json.loads(chart_data_path.read_text(encoding="utf-8")), devices, observations)
        staged: dict[str, Path] = {}
        for fmt in formats:
            destination = temp / f"{BASE_NAME}.{fmt}"
            if fmt == "png":
                compute_chart.render_png(destination, observations, cutoff)
                compute_chart.verify_png(destination)
            elif fmt == "svg":
                compute_chart.render_svg(destination, observations, cutoff)
                compute_chart.verify_svg(destination, eligible)
            elif fmt == "pdf":
                raw = temp / "raw.pdf"
                compute_chart.render_pdf_raw(raw, observations, cutoff)
                compute_chart.structure_pdf(raw, destination, observations)
                compute_chart.verify_pdf(destination, eligible)
            elif fmt == "eps":
                compute_chart.render_eps(destination, observations, cutoff)
                compute_chart.verify_eps(destination, eligible)
            staged[fmt] = destination
        install_atomic(devices_csv, DEVICES_CSV)
        install_atomic(observations_csv, OBSERVATIONS_CSV)
        install_atomic(chart_data_path, CHART_DATA_JSON)
        for fmt, source in staged.items():
            install_atomic(source, OUT_DIR / f"{BASE_NAME}.{fmt}")
    print(f"validated {len(devices)} devices and {len(observations)} metric rows")
    print(f"charted {len(eligible)} cited observations with {len(compute_chart.visible_marker_observations(eligible))} visible markers")
    for metric, count in sorted(compute_chart.metric_counts(observations).items()):
        print(f"  {metric}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formats", nargs="+", choices=FORMATS, default=list(FORMATS))
    args = parser.parse_args()
    generate(list(dict.fromkeys(args.formats)))


if __name__ == "__main__":
    main()
