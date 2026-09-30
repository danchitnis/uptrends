"""Matplotlib renderer and semantic exports for the uP Trend Chart."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    ContentStream,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


WIDTH = 2300
HEIGHT = 1500
PLOT_LEFT = 150
PLOT_RIGHT = 1880
PLOT_TOP = 110
PLOT_BOTTOM = 1220
SERIES_LABEL_X = 1920
X_TICK_Y = 1270
X_TITLE_Y = 1320
X_MIN = 1970.0
X_MAX = 2026.75
Y_MIN = 0.2
Y_MAX = 3.0e11
POINT_OPACITY = 0.72
FONT_STACK = "Arial, Helvetica, sans-serif"
SVG_NS = "http://www.w3.org/2000/svg"
X_TICKS = (1971, 1980, 1990, 2000, 2010, 2020, 2026)
Y_EXPONENTS = tuple(range(0, 12))
MULTICORE_SERIES = ("multi-core-index",)
MARKER_NOTE = "Filled markers: GPU or GPU component; outlined: CPU/other. FP32 shapes: discrete GPU triangle, integrated GPU square, APU GPU diamond."
NOTES_Y = (1390, 1423, 1456)

# The factors are shown beside the series, so plotted ordinates can be read
# from the logarithmic ticks. Source values and units are never modified.
DISPLAY_SCALES = {
    "transistors_million": 1_000_000.0,
    "cpu_multicore_index": 10_000.0,
    "fp32_dense_peak_gflops": 100.0,
    "cpu_single_thread_normalized": 1_000.0,
    "frequency_ghz": 1_000.0,
    "power_w": 1.0,
    "cpu_physical_cores": 1.0,
}

SERIES_ORDER = (
    "transistors",
    "single-thread",
    "frequency",
    "power",
    "core-count",
    *MULTICORE_SERIES,
    "fp32-integrated-gpu",
    "fp32-discrete-gpu",
    "fp32-manycore",
)
SERIES_LABELS = {
    "transistors": "Transistors (count)",
    "single-thread": "CPU single-core performance index (plotted x 10^3)",
    "frequency": "Clock frequency (MHz)",
    "power": "Power (W; reported scope, plotted x 1)",
    "core-count": "CPU physical cores (cores per package, plotted x 1)",
    "multi-core-index": "CPU multi-core throughput: estimated cross-suite index (one chip; 2003=100, plotted x 10^4)",
    "fp32-integrated-gpu": "Dense FP32 peak: integrated GPU (GFLOP/s, plotted x 100)",
    "fp32-discrete-gpu": "Dense FP32 peak: discrete GPU (GFLOP/s, plotted x 100)",
    "fp32-manycore": "Dense FP32 peak: GPU component of APU (GFLOP/s, plotted x 100)",
}
SERIES_COLOURS = {
    "transistors": "#C76500",
    "single-thread": "#001DBB",
    "frequency": "#078A16",
    "power": "#C00000",
    "core-count": "#505050",
    "multi-core-index": "#111111",
    "fp32-integrated-gpu": "#6F3FA0",
    "fp32-discrete-gpu": "#6F3FA0",
    "fp32-manycore": "#6F3FA0",
}
SERIES_SHAPES = {
    "transistors": "triangle",
    "single-thread": "circle",
    "frequency": "square",
    "power": "triangle-down",
    "core-count": "circle",
    "multi-core-index": "diamond",
    "fp32-integrated-gpu": "square",
    "fp32-discrete-gpu": "triangle",
    "fp32-manycore": "diamond",
}
DIRECT_LABELS = (
    ("transistors", ("Transistors", "(count)"), 3.3e10),
    ("multi-core-index", ("CPU Multi-Core", "Throughput Index", "(2003=100; x 10^4)"), 1.0e9),
    ("fp32", ("Dense FP32 Peak", "(GPU only)", "(GFLOP/s x 100)"), 2.0e7),
    ("single-thread", ("CPU Single-Core", "Performance Index", "(x 10^3)"), 3.3e5),
    ("frequency", ("Frequency (MHz)",), 3.2e3),
    ("power", ("Power (Watts)",), 6.0e2),
    ("core-count", ("CPU Physical Cores", "(cores per package)"), 3.0e1),
)


def series_key(obs: Any) -> str:
    metric = obs.metric
    if metric == "transistors_million":
        return "transistors"
    if metric == "frequency_ghz":
        return "frequency"
    if metric == "power_w":
        return "power"
    if metric == "cpu_physical_cores":
        return "core-count"
    if metric == "cpu_multicore_index":
        return "multi-core-index"
    if metric == "gpu_compute_units":
        return "gpu-units"
    if metric == "cpu_single_thread_normalized":
        return "single-thread"
    if metric != "fp32_dense_peak_gflops":
        raise ValueError(f"metric is not plotted: {metric}")
    if not obs.gpu_component:
        raise ValueError(f"CPU FP32 is not plotted: {obs.observation_id}")
    if obs.device_class == "gpu":
        return "fp32-discrete-gpu"
    if obs.device_class == "soc":
        return "fp32-integrated-gpu"
    if obs.device_class == "manycore":
        return "fp32-manycore"
    raise ValueError(f"unclassified GPU FP32 observation: {obs.observation_id}")


def display_value(obs: Any) -> float:
    return float(obs.value) * DISPLAY_SCALES.get(obs.metric, 1.0)


def eligible(observations: Iterable[Any]) -> list[Any]:
    return [
        obs for obs in observations
        if obs.value is not None
        and obs.validation_status == "eligible"
        and (obs.metric != "fp32_dense_peak_gflops" or obs.gpu_component)
        and Y_MIN <= display_value(obs) <= Y_MAX
    ]


def visible_marker_observations(observations: Iterable[Any]) -> list[Any]:
    return list(observations)


def ordered_series(observations: Iterable[Any]) -> list[str]:
    present = {series_key(obs) for obs in observations}
    return [key for key in SERIES_ORDER if key in present]


def map_y(value: float) -> float:
    lo, hi = math.log10(Y_MIN), math.log10(Y_MAX)
    return PLOT_BOTTOM - (math.log10(value) - lo) / (hi - lo) * (PLOT_BOTTOM - PLOT_TOP)


def marker_radius(key: str) -> float:
    if key in MULTICORE_SERIES:
        return 10.5
    if key == "core-count":
        return 5.0
    if key.startswith("fp32-"):
        return 6.0
    return 7.0


TITLE = "uP Trend Chart"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def _text(fig: Figure, width: int, height: int, x: float, y: float,
          value: str, size: float, *, gid: str, color: str = "#111111",
          weight: str = "normal", align: str = "left") -> None:
    artist = fig.text(x / width, 1 - y / height, value, fontsize=size,
                      color=color, weight=weight, ha=align, va="baseline",
                      fontfamily="DejaVu Sans")
    artist.set_gid(gid)


def _figure(observations: list[Any], cutoff: str, *, opaque: bool = False) -> Figure:
    width, height = WIDTH, HEIGHT
    left, right = PLOT_LEFT, PLOT_RIGHT
    top, bottom = PLOT_TOP, PLOT_BOTTOM
    fig = Figure(figsize=(width / 72, height / 72), dpi=72, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes((left / width, (height - bottom) / height,
                       (right - left) / width, (bottom - top) / height))
    ax.set_xlim(X_MIN, X_MAX)
    ax.set_yscale("log")
    ax.set_ylim(Y_MIN, Y_MAX)
    ax.set_axis_off()
    for exponent in Y_EXPONENTS:
        line = ax.axhline(10.0 ** exponent, color="#AAB0B6", linewidth=1.4,
                          linestyle=(0, (2, 5)), zorder=0)
        line.set_gid(f"grid-y-{exponent}")
    for year in X_TICKS:
        line = ax.axvline(year, color="#AAB0B6", linewidth=1.4,
                          linestyle=(0, (2, 5)), zorder=0)
        line.set_gid(f"grid-x-{year}")
    frame = Rectangle((0, 0), 1, 1, transform=ax.transAxes, fill=False,
                      edgecolor="#111111", linewidth=4.5,
                      zorder=4)
    frame.set_gid("plot-frame")
    ax.add_patch(frame)
    markers = visible_marker_observations(eligible(observations))
    shape_markers = {"circle": "o", "square": "s", "triangle": "^",
                     "triangle-down": "v", "diamond": "D"}
    for key in ordered_series(markers):
        for obs in (item for item in markers if series_key(item) == key):
            colour = SERIES_COLOURS[key]
            rgb = tuple(int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
            # PostScript cannot preserve transparency, so flatten its marker
            # colour against the chart's white background.
            if opaque:
                rgb = tuple(1 - (1 - channel) * POINT_OPACITY for channel in rgb)
            edge = (*rgb, 1.0 if opaque else POINT_OPACITY)
            face = edge if obs.filled else (1, 1, 1, 1)
            radius = marker_radius(key) * 1.4
            line, = ax.plot([obs.release_year], [display_value(obs)],
                            linestyle="None", marker=shape_markers[SERIES_SHAPES[key]],
                            markersize=radius * 2, markerfacecolor=face,
                            markeredgecolor=edge, markeredgewidth=3.3,
                            zorder=3)
            line.set_gid(obs.observation_id)
    _text(fig, width, height, (left + right) / 2, 73,
          TITLE, 56, gid="chart-title-text",
          weight="bold", align="center")
    for year in X_TICKS:
        x = left + (year - X_MIN) / (X_MAX - X_MIN) * (right - left)
        _text(fig, width, height, x, X_TICK_Y,
              str(year), 36, gid=f"x-label-{year}", align="center")
    _text(fig, width, height, (left + right) / 2,
          X_TITLE_Y, "Year",
          38, gid="x-axis-title", align="center", weight="bold")
    for exponent in Y_EXPONENTS:
        y = map_y(10.0 ** exponent)
        _text(fig, width, height, left - 62, y + 10,
              "10", 34, gid=f"y-base-{exponent}", align="right")
        _text(fig, width, height, left - 60, y - 6,
              str(exponent), 22, gid=f"y-exp-{exponent}")
    label_x = SERIES_LABEL_X
    label_step = 42
    for identifier, lines, value in DIRECT_LABELS:
        colour = SERIES_COLOURS.get(identifier, SERIES_COLOURS["fp32-discrete-gpu"])
        for index, line in enumerate(lines):
            value_text = line.replace("x 10^4", "× 10⁴").replace("x 10^3", "× 10³")
            value_text = value_text.replace("x 100", "× 100")
            _text(fig, width, height, label_x, map_y(value) + index * label_step,
                  value_text, 34,
                  gid=f"label-{identifier}-{index}", color=colour, weight="bold")
    notes = (
        "Historical data: Horowitz et al. and Rupp; modern additions use cited manufacturer and SPEC sources.",
        MARKER_NOTE,
        f"Shipping through {cutoff}. Series-specific units and multipliers; compare trends, not cross-series heights.",
    )
    for index, (value, y) in enumerate(zip(notes, NOTES_Y, strict=True)):
        _text(fig, width, height, left, y, value,
              20, gid=f"note-{index}", color="#333333")
    return fig


def render_raw(path: Path, observations: list[Any], cutoff: str, fmt: str) -> None:
    with matplotlib.rc_context({"svg.fonttype": "none", "svg.hashsalt": "uptrend",
                                "pdf.fonttype": 42, "ps.fonttype": 42,
                                "text.usetex": False}):
        fig = _figure(observations, cutoff, opaque=fmt == "eps")
        metadata = ({"Title": TITLE, "Date": None} if fmt == "svg" else
                    {"Title": TITLE, "CreationDate": None} if fmt == "pdf" else
                    {"Title": TITLE} if fmt == "png" else None)
        fig.savefig(path, format=fmt, dpi=72, bbox_inches=None, pad_inches=0,
                    metadata=metadata)
        if fmt == "eps":
            body = path.read_text(encoding="latin-1")
            body = re.sub(r"^%%CreationDate:.*$", f"%%CreationDate: {cutoff}",
                          body, flags=re.MULTILINE)
            body = re.sub(r"^%%Title:.*$", f"%%Title: {TITLE}",
                          body, flags=re.MULTILINE)
            path.write_text(body, encoding="latin-1")


def _element(tag: str, attrs: dict[str, str] | None = None,
             value: str | None = None) -> ET.Element:
    element = ET.Element(f"{{{SVG_NS}}}{tag}", attrs or {})
    element.text = value
    return element


def structure_svg(raw_path: Path, output_path: Path, observations: list[Any]) -> None:
    root = ET.parse(raw_path).getroot()
    metadata = root.find(f"{{{SVG_NS}}}metadata")
    if metadata is not None:
        root.remove(metadata)
    root.set("width", str(WIDTH))
    root.set("height", str(HEIGHT))
    root.set("viewBox", f"0 0 {WIDTH} {HEIGHT}")
    figure = root.find(f".//{{{SVG_NS}}}g[@id='figure_1']")
    if figure is None:
        raise RuntimeError("Matplotlib SVG is missing its figure")
    source_by_id = {item.get("id"): item for item in figure.iter()
                    if item.get("id")}
    path_by_id = {item.get("id"): item for item in figure.iter(f"{{{SVG_NS}}}path")
                  if item.get("id")}
    background_source = source_by_id.get("patch_1")
    if background_source is None:
        raise RuntimeError("Matplotlib SVG has no background")
    for child in list(figure):
        figure.remove(child)
    figure.set("id", "figure")
    figure.set("class", "uptrend-figure")
    background = _element("g", {"id": "figure-background"})
    background.extend(list(background_source))
    figure.append(background)
    title = _element("g", {"id": "chart-title", "class": "chart-component"})
    title_text = source_by_id["chart-title-text"].find(f"{{{SVG_NS}}}text")
    if title_text is None:
        raise RuntimeError("Matplotlib SVG is missing its chart title text")
    title.append(title_text)
    figure.append(title)
    chart = _element("g", {"id": "chart", "class": "overlaid-log-chart"})
    grid = _element("g", {"id": "chart-grid", "class": "chart-grid"})
    for exponent in Y_EXPONENTS:
        grid.append(source_by_id[f"grid-y-{exponent}"])
    for year in X_TICKS:
        grid.append(source_by_id[f"grid-x-{year}"])
    chart.append(grid)
    axes = _element("g", {"id": "chart-axes", "class": "chart-axes"})
    axes.append(source_by_id["plot-frame"])
    x_axis = _element("g", {"id": "x-axis", "class": "axis"})
    for year in X_TICKS:
        x_axis.append(source_by_id[f"x-label-{year}"])
    x_axis.append(source_by_id["x-axis-title"])
    axes.append(x_axis)
    y_axis = _element("g", {"id": "y-axis", "class": "axis"})
    for exponent in Y_EXPONENTS:
        label = _element("g", {"id": f"y-label-{exponent}"})
        label.append(source_by_id[f"y-base-{exponent}"])
        label.append(source_by_id[f"y-exp-{exponent}"])
        y_axis.append(label)
    axes.append(y_axis)
    chart.append(axes)
    series_parent = _element("g", {"id": "chart-series", "class": "chart-series"})
    markers = visible_marker_observations(eligible(observations))
    for key in ordered_series(markers):
        series = _element("g", {"id": f"series-{key}", "class": "data-series",
                                "data-series": key})
        series.append(_element("title", value=SERIES_LABELS[key]))
        for obs in (item for item in markers if series_key(item) == key):
            raw = source_by_id[obs.observation_id]
            use = next(raw.iter(f"{{{SVG_NS}}}use"), None)
            if use is None:
                raise RuntimeError(f"SVG marker is missing: {obs.observation_id}")
            href = use.get(f"{{{XLINK_NS}}}href", "").lstrip("#")
            path = path_by_id.get(href)
            if path is None:
                raise RuntimeError(f"SVG marker path is missing: {obs.observation_id}")
            device = _element("g", {"id": f"device-{key}-{obs.device_id}",
                                    "class": "device", "data-device-id": obs.device_id})
            point = _element("g", {"id": obs.observation_id, "class": "data-point",
                                   "data-device-id": obs.device_id, "data-series": key,
                                   "data-metric": obs.metric,
                                   "data-year": f"{obs.release_year:.6f}",
                                   "data-value": f"{obs.value:.12g}",
                                   "data-display-value": f"{display_value(obs):.12g}",
                                   "data-confidence": obs.confidence,
                                   "data-value-kind": obs.value_kind,
                                   "data-raw-benchmark-suite": obs.raw_benchmark_suite,
                                   "data-raw-benchmark-value": obs.raw_benchmark_value})
            point.append(_element("title", value=f"{obs.device_name}: {obs.value:g} {obs.unit}"))
            colour = SERIES_COLOURS[key]
            point.append(_element("path", {
                "d": path.get("d", ""),
                "transform": f"translate({use.get('x')},{use.get('y')})",
                "fill": colour if obs.filled else "white",
                "fill-opacity": f"{POINT_OPACITY:.2f}" if obs.filled else "1",
                "stroke": colour,
                "stroke-opacity": f"{POINT_OPACITY:.2f}",
                "stroke-width": "4.5" if key in MULTICORE_SERIES else "3.3",
            }))
            device.append(point)
            series.append(device)
        series_parent.append(series)
    chart.append(series_parent)
    labels = _element("g", {"id": "chart-labels", "class": "chart-labels"})
    for identifier, lines, _ in DIRECT_LABELS:
        group = _element("g", {"id": f"label-{identifier}",
                               "class": "direct-series-label"})
        for index in range(len(lines)):
            group.append(source_by_id[f"label-{identifier}-{index}"])
        labels.append(group)
    chart.append(labels)
    figure.append(chart)
    notes = _element("g", {"id": "chart-notes", "class": "chart-notes"})
    for index in range(3):
        notes.append(source_by_id[f"note-{index}"])
    figure.append(notes)
    for item in root.iter(f"{{{SVG_NS}}}text"):
        item.set("font-family", FONT_STACK)
        style = item.get("style", "")
        item.set("style", re.sub(r"font-family:\s*[^;]+",
                                  f"font-family: {FONT_STACK}", style))
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)


def structure_pdf(raw_path: Path, output_path: Path, observations: list[Any]) -> None:
    reader = PdfReader(raw_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    page = writer.pages[0]
    content = ContentStream(page.get_contents(), writer)
    markers = [obs for key in ordered_series(eligible(observations))
               for obs in eligible(observations) if series_key(obs) == key]
    paint_indices = [index for index, (_, operator) in enumerate(content.operations)
                     if operator in (b"B", b"B*")]
    if len(paint_indices) != len(markers):
        raise RuntimeError(f"expected {len(markers)} PDF marker paints, found {len(paint_indices)}")
    resources = page[NameObject("/Resources")].get_object()
    properties = DictionaryObject()
    resources[NameObject("/Properties")] = properties
    layer_refs = {}
    for index, key in enumerate(ordered_series(markers), 1):
        ref = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/OCG"),
            NameObject("/Name"): TextStringObject(SERIES_LABELS[key]),
        }))
        layer_refs[key] = ref
        properties[NameObject(f"/Series{index}")] = ref
    prop_for_key = {key: NameObject(f"/Series{index}")
                    for index, key in enumerate(ordered_series(markers), 1)}
    root_structure = DictionaryObject({NameObject("/Type"): NameObject("/StructTreeRoot")})
    root_ref = writer._add_object(root_structure)
    series_refs = ArrayObject()
    point_refs = ArrayObject()
    for key in ordered_series(markers):
        section = DictionaryObject({
            NameObject("/Type"): NameObject("/StructElem"),
            NameObject("/S"): NameObject("/Sect"),
            NameObject("/P"): root_ref,
            NameObject("/T"): TextStringObject(SERIES_LABELS[key]),
        })
        section_ref = writer._add_object(section)
        children = ArrayObject()
        for obs in (item for item in markers if series_key(item) == key):
            mcid = len(point_refs)
            figure = DictionaryObject({
                NameObject("/Type"): NameObject("/StructElem"),
                NameObject("/S"): NameObject("/Figure"),
                NameObject("/P"): section_ref,
                NameObject("/Pg"): page.indirect_reference,
                NameObject("/K"): NumberObject(mcid),
                NameObject("/T"): TextStringObject(obs.observation_id),
                NameObject("/Alt"): TextStringObject(f"{obs.device_name}: {obs.value:g} {obs.unit}"),
            })
            point_ref = writer._add_object(figure)
            point_refs.append(point_ref)
            children.append(point_ref)
        section[NameObject("/K")] = children
        series_refs.append(section_ref)
    root_structure[NameObject("/K")] = series_refs
    root_structure[NameObject("/ParentTree")] = writer._add_object(DictionaryObject({
        NameObject("/Nums"): ArrayObject([NumberObject(0), point_refs]),
    }))
    root_structure[NameObject("/ParentTreeNextKey")] = NumberObject(1)
    marker_ranges = []
    for paint_index in paint_indices:
        nested = 0
        for start in range(paint_index, -1, -1):
            operator = content.operations[start][1]
            if operator == b"Q":
                nested += 1
            elif operator == b"q":
                if nested == 0:
                    break
                nested -= 1
        else:
            raise RuntimeError("PDF marker has no graphics-state start")
        nested = 0
        for end in range(start, len(content.operations)):
            operator = content.operations[end][1]
            if operator == b"q":
                nested += 1
            elif operator == b"Q":
                nested -= 1
                if nested == 0:
                    break
        else:
            raise RuntimeError("PDF marker has no graphics-state end")
        if not start <= paint_index <= end:
            raise RuntimeError("PDF marker paint is outside its graphics state")
        marker_ranges.append((start, end))
    if any(first[1] >= second[0]
           for first, second in zip(marker_ranges, marker_ranges[1:])):
        raise RuntimeError("PDF marker graphics states overlap")
    starts = {start: (mcid, obs) for (start, _), (mcid, obs)
              in zip(marker_ranges, enumerate(markers), strict=True)}
    ends = {end for _, end in marker_ranges}
    rewritten = []
    for index, operation in enumerate(content.operations):
        marker = starts.get(index)
        if marker is not None:
            mcid, obs = marker
            info = DictionaryObject({
                NameObject("/MCID"): NumberObject(mcid),
                NameObject("/ID"): TextStringObject(obs.observation_id),
                NameObject("/ActualText"): TextStringObject(f"{obs.device_name}: {obs.value:g} {obs.unit}"),
            })
            rewritten.extend((
                ([NameObject("/OC"), prop_for_key[series_key(obs)]], b"BDC"),
                ([NameObject("/Figure"), info], b"BDC"),
            ))
        rewritten.append(operation)
        if index in ends:
            rewritten.extend((([], b"EMC"), ([], b"EMC")))
    content.operations = rewritten
    page[NameObject("/Contents")] = writer._add_object(content)
    page[NameObject("/StructParents")] = NumberObject(0)
    layers = ArrayObject(list(layer_refs.values()))
    writer.root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): layers,
        NameObject("/D"): DictionaryObject({
            NameObject("/Order"): layers,
            NameObject("/ON"): layers,
        }),
    })
    writer.root_object[NameObject("/StructTreeRoot")] = root_ref
    writer.root_object[NameObject("/MarkInfo")] = DictionaryObject({
        NameObject("/Marked"): BooleanObject(True),
    })
    writer.root_object[NameObject("/Lang")] = TextStringObject("en-GB")
    writer.root_object[NameObject("/PageMode")] = NameObject("/UseOC")
    with output_path.open("wb") as stream:
        writer.write(stream)


def structure_eps(raw_path: Path, output_path: Path, observations: list[Any]) -> None:
    lines = raw_path.read_text(encoding="latin-1").splitlines()
    markers = [obs for key in ordered_series(eligible(observations))
               for obs in eligible(observations) if series_key(obs) == key]
    starts = [index for index, line in enumerate(lines) if line == "/o {"]
    if len(starts) != len(markers):
        raise RuntimeError(f"expected {len(markers)} EPS markers, found {len(starts)}")
    ends = {}
    for start in starts:
        definition = next(index for index in range(start + 1, len(lines))
                          if lines[index] == "} bind def")
        invocation = definition + 1
        if not re.fullmatch(r"[-+0-9.]+ [-+0-9.]+ o", lines[invocation]):
            raise RuntimeError("EPS marker invocation was not found")
        ends[start] = invocation
    start_to_point = dict(zip(starts, markers, strict=True))
    end_to_point = {ends[start]: obs for start, obs in start_to_point.items()}
    last_of_series = {
        obs.observation_id
        for index, obs in enumerate(markers)
        if index + 1 == len(markers) or series_key(markers[index + 1]) != series_key(obs)
    }
    output = []
    previous_key = None
    for index, line in enumerate(lines):
        obs = start_to_point.get(index)
        if obs is not None:
            key = series_key(obs)
            if previous_key != key:
                output.append(f"%%BeginObject: series-{key}")
                previous_key = key
            output.append(f"%%BeginObject: {obs.observation_id}")
            output.append(f"%%PointData: device={obs.device_id} metric={obs.metric} year={obs.release_year:.6f} value={obs.value:.12g}")
        output.append(line)
        if index in end_to_point:
            output.append("%%EndObject")
            if end_to_point[index].observation_id in last_of_series:
                output.append("%%EndObject")
    output_path.write_text("\n".join(output) + "\n", encoding="latin-1")


def verify_png(path: Path) -> None:
    with Image.open(path) as image:
        if image.format != "PNG" or image.size != (WIDTH, HEIGHT):
            raise RuntimeError("PNG dimensions or format changed")


def verify_svg(path: Path, expected: list[Any]) -> None:
    root = ET.parse(path).getroot()
    if (root.get("width"), root.get("height")) != (str(WIDTH), str(HEIGHT)):
        raise RuntimeError("SVG dimensions changed")
    if any(item.tag == f"{{{SVG_NS}}}image" for item in root.iter()):
        raise RuntimeError("SVG contains a raster image")
    groups = list(root.iter(f"{{{SVG_NS}}}g"))
    point_groups = [item for item in groups if item.get("class") == "data-point"]
    actual = {item.get("id") for item in point_groups}
    wanted = {obs.observation_id for obs in visible_marker_observations(expected)}
    if actual != wanted or len(point_groups) != len(wanted):
        raise RuntimeError("SVG point IDs do not match the chart data")
    by_id = {item.get("id"): item for item in point_groups}
    for obs in expected:
        point = by_id[obs.observation_id]
        if (point.get("data-device-id") != obs.device_id
                or point.get("data-metric") != obs.metric
                or point.get("data-series") != series_key(obs)
                or point.get("data-year") != f"{obs.release_year:.6f}"
                or point.get("data-value") != f"{obs.value:.12g}"):
            raise RuntimeError(f"SVG point metadata changed: {obs.observation_id}")
        if len(point.findall(f"{{{SVG_NS}}}path")) != 1:
            raise RuntimeError(f"SVG marker path changed: {obs.observation_id}")
    for identifier in ("chart", "chart-grid", "chart-series", "plot-frame", "chart-title"):
        if root.find(f".//*[@id='{identifier}']") is None:
            raise RuntimeError(f"SVG is missing {identifier}")


def verify_pdf(path: Path, expected: list[Any]) -> None:
    reader = PdfReader(path)
    if len(reader.pages) != 1:
        raise RuntimeError("PDF must contain one page")
    page = reader.pages[0]
    if (float(page.mediabox.width), float(page.mediabox.height)) != (WIDTH, HEIGHT):
        raise RuntimeError("PDF dimensions changed")
    resources = page["/Resources"].get_object()
    xobjects = resources.get("/XObject", {})
    xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
    if any(ref.get_object().get("/Subtype") == "/Image" for ref in xobjects.values()):
        raise RuntimeError("PDF contains a raster image")
    fonts = resources.get("/Font", {}).get_object()
    if not fonts:
        raise RuntimeError("PDF contains no text fonts")
    for font_ref in fonts.values():
        font = font_ref.get_object()
        if font.get("/Subtype") == "/Type0":
            font = font["/DescendantFonts"][0].get_object()
        descriptor = font.get("/FontDescriptor")
        descriptor = descriptor.get_object() if descriptor is not None else None
        if not descriptor or not any(descriptor.get(key) for key in
                                     ("/FontFile", "/FontFile2", "/FontFile3")):
            raise RuntimeError("PDF contains an unembedded font")
    root = reader.trailer["/Root"]
    layers = root.get("/OCProperties")
    structure = root.get("/StructTreeRoot")
    if layers is None or structure is None:
        raise RuntimeError("PDF is missing layers or tagged structure")
    if len(layers.get_object()["/OCGs"]) != len(ordered_series(expected)):
        raise RuntimeError("PDF series layer count changed")
    content = ContentStream(reader.pages[0].get_contents(), reader)
    tagged = [ops for ops, operator in content.operations
              if operator == b"BDC" and ops and str(ops[0]) == "/Figure"]
    if len(tagged) != len(visible_marker_observations(expected)):
        raise RuntimeError("PDF tagged point count changed")
    sections = structure.get_object().get("/K", [])
    if len(sections) != len(ordered_series(expected)):
        raise RuntimeError("PDF structure tree series count changed")
    for section, key in zip(sections, ordered_series(expected), strict=True):
        figures = section.get_object().get("/K", [])
        wanted_ids = [obs.observation_id for obs in expected if series_key(obs) == key]
        actual_ids = [str(item.get_object().get("/T")) for item in figures]
        if actual_ids != wanted_ids:
            raise RuntimeError(f"PDF point structure changed for {key}")


def verify_eps(path: Path, expected: list[Any]) -> None:
    body = path.read_text(encoding="latin-1")
    if not re.search(rf"^%%BoundingBox: 0 0 {WIDTH} {HEIGHT}$", body, re.MULTILINE):
        raise RuntimeError("EPS dimensions changed")
    code = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("%"))
    if re.search(r"\b(?:image|colorimage|imagemask)\b", code, re.I):
        raise RuntimeError("EPS contains a raster image")
    if body.count("%%BeginObject:") != body.count("%%EndObject"):
        raise RuntimeError("EPS object markers are unbalanced")
    if body.count("%%PointData:") != len(visible_marker_observations(expected)):
        raise RuntimeError("EPS point metadata count changed")
    for key in ordered_series(expected):
        if body.count(f"%%BeginObject: series-{key}\n") != 1:
            raise RuntimeError(f"EPS series group is missing: {key}")
    for obs in expected:
        if body.count(f"%%BeginObject: {obs.observation_id}\n") != 1:
            raise RuntimeError(f"EPS point object is missing: {obs.observation_id}")


def metric_counts(observations: Iterable[Any]) -> Counter[str]:
    return Counter(obs.metric for obs in eligible(observations))
