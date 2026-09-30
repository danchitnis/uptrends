#!/usr/bin/env python3
"""Generate the data exports and uP Trend Chart."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import uptrend_renderer


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "uptrend"
PROVENANCE = OUT_DIR / "uptrend-provenance.json"
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
DEVICES_CSV = OUT_DIR / "uptrend-devices.csv"
OBSERVATIONS_CSV = OUT_DIR / "uptrend-observations.csv"
CHART_DATA_JSON = OUT_DIR / "uptrend-chart-data.json"
BASE_NAME = "uptrend-chart"
FORMATS = ("png", "svg", "pdf", "eps")
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
    eligible = uptrend_renderer.eligible(observations)
    plotted = uptrend_renderer.visible_marker_observations(eligible)
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
                    "series": uptrend_renderer.series_key(obs),
                    "x_year": x_year,
                    "value": obs.value,
                    "display_value": uptrend_renderer.display_value(obs),
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
    expected = {obs.observation_id for obs in uptrend_renderer.visible_marker_observations(uptrend_renderer.eligible(observations))}
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
    eligible_ids = {obs.observation_id for obs in uptrend_renderer.eligible(observations)}
    if seen_guides != eligible_ids - expected:
        raise RuntimeError("chart guide anchors do not match the rendered chart")


def install_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def generate(formats: Iterable[str]) -> None:
    devices, cutoff = load_devices()
    observations = build_observations(devices)
    validate(devices, observations, cutoff)
    eligible = uptrend_renderer.eligible(observations)
    chart_data = build_chart_data(devices, observations, cutoff)
    with tempfile.TemporaryDirectory(prefix="uptrend-", dir=ROOT) as temp_name:
        temp = Path(temp_name)
        devices_csv, observations_csv = write_csvs(temp, devices, observations)
        chart_data_path = temp / CHART_DATA_JSON.name
        chart_data_path.write_text(json.dumps(chart_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        verify_chart_data(json.loads(chart_data_path.read_text(encoding="utf-8")), devices, observations)
        staged: dict[str, Path] = {}
        for fmt in formats:
            destination = temp / f"{BASE_NAME}.{fmt}"
            raw = temp / f"raw.{fmt}"
            uptrend_renderer.render_raw(raw, observations, cutoff, fmt)
            if fmt == "svg":
                uptrend_renderer.structure_svg(raw, destination, observations)
                uptrend_renderer.verify_svg(destination, eligible)
            elif fmt == "pdf":
                uptrend_renderer.structure_pdf(raw, destination, observations)
                uptrend_renderer.verify_pdf(destination, eligible)
            elif fmt == "eps":
                uptrend_renderer.structure_eps(raw, destination, observations)
                uptrend_renderer.verify_eps(destination, eligible)
            else:
                os.replace(raw, destination)
                uptrend_renderer.verify_png(destination)
            staged[fmt] = destination
        install_atomic(devices_csv, DEVICES_CSV)
        install_atomic(observations_csv, OBSERVATIONS_CSV)
        install_atomic(chart_data_path, CHART_DATA_JSON)
        for fmt, source in staged.items():
            install_atomic(source, OUT_DIR / f"{BASE_NAME}.{fmt}")
    print(f"validated {len(devices)} devices and {len(observations)} metric rows")
    print(f"charted {len(eligible)} cited observations with {len(uptrend_renderer.visible_marker_observations(eligible))} visible markers")
    for metric, count in sorted(uptrend_renderer.metric_counts(observations).items()):
        print(f"  {metric}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formats", nargs="+", choices=FORMATS, default=list(FORMATS))
    args = parser.parse_args()
    generate(list(dict.fromkeys(args.formats)))


if __name__ == "__main__":
    main()
