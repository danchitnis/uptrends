"""Single-field vector renderer for the 55-year CPU/GPU compute chart."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
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


WIDTH = 1800
HEIGHT = 1695
# The SVG is the screen/presentation edition; print and PNG keep their own
# established geometry above and below. Do not scale text glyphs to widen it.
SVG_WIDTH = 2300
SVG_HEIGHT = 1500
SVG_PLOT_LEFT = 150
SVG_PLOT_RIGHT = 1880
SVG_PLOT_TOP = 110
SVG_PLOT_BOTTOM = 1220
SVG_SERIES_LABEL_X = 1920
SVG_X_TICK_Y = 1270
SVG_X_TITLE_Y = 1320
PLOT_LEFT = 135
PLOT_RIGHT = 1520
SERIES_LABEL_X = PLOT_RIGHT + 25
PLOT_TOP = 145
PLOT_BOTTOM = 1410
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
MULTICORE_NOTE = "Black diamonds: unconnected estimates of one-chip CPU throughput, 2003 = 100; suite bridges are not official SPEC conversions."
MARKER_NOTE = "Filled markers: GPU or GPU component; outlined: CPU/other. FP32 shapes: discrete GPU triangle, integrated GPU square, APU GPU diamond."
SINGLE_CORE_NOTE = "Apple M-series single-core: Geekbench 7/M1-anchored index, not a SPEC score."
DISPLAY_NOTE = "Each series uses the unit and multiplier printed beside it. The shared log axis has no common unit: compare trends, not cross-series heights."
X_TICK_Y = PLOT_BOTTOM + 42
X_TITLE_Y = PLOT_BOTTOM + 87
NOTES_Y = (PLOT_BOTTOM + 140, PLOT_BOTTOM + 170, PLOT_BOTTOM + 200, PLOT_BOTTOM + 230, PLOT_BOTTOM + 260)
PNG_X_TICK_Y = PLOT_BOTTOM + 17
PNG_X_TITLE_Y = PLOT_BOTTOM + 55
PNG_NOTES_Y = (PLOT_BOTTOM + 123, PLOT_BOTTOM + 153, PLOT_BOTTOM + 183, PLOT_BOTTOM + 213, PLOT_BOTTOM + 243)

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


def map_x(year: float) -> float:
    return PLOT_LEFT + (year - X_MIN) / (X_MAX - X_MIN) * (PLOT_RIGHT - PLOT_LEFT)


def map_y(value: float) -> float:
    lo, hi = math.log10(Y_MIN), math.log10(Y_MAX)
    return PLOT_BOTTOM - (math.log10(value) - lo) / (hi - lo) * (PLOT_BOTTOM - PLOT_TOP)


def svg_map_x(year: float) -> float:
    return SVG_PLOT_LEFT + (year - X_MIN) / (X_MAX - X_MIN) * (SVG_PLOT_RIGHT - SVG_PLOT_LEFT)


def svg_map_y(value: float) -> float:
    lo, hi = math.log10(Y_MIN), math.log10(Y_MAX)
    return SVG_PLOT_BOTTOM - (math.log10(value) - lo) / (hi - lo) * (SVG_PLOT_BOTTOM - SVG_PLOT_TOP)


def marker_points(shape: str, x: float, y: float, radius: float = 7.0) -> list[tuple[float, float]]:
    if shape == "square":
        return [(x-radius, y-radius), (x+radius, y-radius), (x+radius, y+radius), (x-radius, y+radius)]
    if shape == "triangle":
        return [(x, y-radius), (x+radius, y+radius), (x-radius, y+radius)]
    if shape == "triangle-down":
        return [(x-radius, y-radius), (x+radius, y-radius), (x, y+radius)]
    return [(x, y-radius), (x+radius, y), (x, y+radius), (x-radius, y)]


def marker_path(shape: str, x: float, y: float, radius: float = 7.0) -> str:
    points = marker_points(shape, x, y, radius)
    return "M " + " L ".join(f"{px:.2f},{py:.2f}" for px, py in points) + " Z"


def marker_radius(key: str) -> float:
    if key in MULTICORE_SERIES:
        return 10.5
    if key == "core-count":
        return 5.0
    if key.startswith("fp32-"):
        return 6.0
    return 7.0


def svg_element(tag: str, attributes: dict[str, str] | None = None, text: str | None = None) -> ET.Element:
    element = ET.Element(f"{{{SVG_NS}}}{tag}", attributes or {})
    element.text = text
    return element


def add_svg_marker(parent: ET.Element, obs: Any) -> None:
    key = series_key(obs)
    colour = SERIES_COLOURS[key]
    x, y = svg_map_x(obs.release_year), svg_map_y(display_value(obs))
    point = svg_element("g", {
        "id": obs.observation_id,
        "class": "data-point",
        "data-device-id": obs.device_id,
        "data-series": key,
        "data-metric": obs.metric,
        "data-year": f"{obs.release_year:.6f}",
        "data-value": f"{obs.value:.12g}",
        "data-display-value": f"{display_value(obs):.12g}",
        "data-confidence": obs.confidence,
        "data-value-kind": obs.value_kind,
        "data-raw-benchmark-suite": obs.raw_benchmark_suite,
        "data-raw-benchmark-value": obs.raw_benchmark_value,
    })
    raw = f"; source {obs.raw_benchmark_suite}={obs.raw_benchmark_value}" if obs.raw_benchmark_suite else ""
    point.append(svg_element("title", text=f"{obs.device_name}: {obs.value:g} {obs.unit} ({obs.value_kind}, {obs.confidence}{raw})"))
    emphasis = key in MULTICORE_SERIES
    radius = marker_radius(key) * 1.4
    style = {
        "stroke": colour,
        "stroke-width": "4.5" if emphasis else "3.3",
        "stroke-opacity": f"{POINT_OPACITY:.2f}",
        "fill": colour if obs.filled else "white",
        "fill-opacity": f"{POINT_OPACITY:.2f}" if obs.filled else "1",
    }
    shape = SERIES_SHAPES[key]
    if shape == "circle":
        point.append(svg_element("circle", {**style, "cx": f"{x:.2f}", "cy": f"{y:.2f}", "r": f"{radius:g}"}))
    else:
        point.append(svg_element("path", {**style, "d": marker_path(shape, x, y, radius)}))
    parent.append(point)


def add_svg_direct_labels(parent: ET.Element) -> None:
    colours = {**SERIES_COLOURS, "fp32": SERIES_COLOURS["fp32-discrete-gpu"]}
    for identifier, lines, value in DIRECT_LABELS:
        group = svg_element("g", {"id": f"label-{identifier}", "class": "direct-series-label"})
        group.append(svg_element("title", text=" ".join(lines)))
        y = svg_map_y(value)
        for index, line in enumerate(lines):
            display_line = line.replace("x 10^4", "× 10⁴").replace("x 10^3", "× 10³").replace("x 100", "× 100")
            group.append(svg_element("text", {"x": str(SVG_SERIES_LABEL_X), "y": f"{y + index*42:.2f}", "font-size": "34", "font-weight": "700", "fill": colours[identifier]}, display_line))
        parent.append(group)


def render_svg(path: Path, observations: list[Any], cutoff: str) -> None:
    ET.register_namespace("", SVG_NS)
    points = eligible(observations)
    root = svg_element("svg", {"width": str(SVG_WIDTH), "height": str(SVG_HEIGHT), "viewBox": f"0 0 {SVG_WIDTH} {SVG_HEIGHT}", "version": "1.1"})
    root.append(svg_element("title", text="55 Years of CPU and GPU Compute Trends"))
    root.append(svg_element("desc", text="One overlaid logarithmic chart using the series-specific units and multipliers printed beside each trend; compare slopes, not cross-series heights."))
    figure = svg_element("g", {"id": "figure", "class": "compute-trends-figure"})
    figure.append(svg_element("rect", {"id": "figure-background", "x": "0", "y": "0", "width": str(SVG_WIDTH), "height": str(SVG_HEIGHT), "fill": "white"}))
    title = svg_element("g", {"id": "chart-title", "class": "chart-component"})
    title.append(svg_element("text", {"x": f"{(SVG_PLOT_LEFT+SVG_PLOT_RIGHT)/2:.2f}", "y": "73", "text-anchor": "middle", "font-size": "56", "font-weight": "700"}, "55 Years of CPU and GPU Compute Trends"))
    figure.append(title)
    chart = svg_element("g", {"id": "chart", "class": "overlaid-log-chart"})
    grid = svg_element("g", {"id": "chart-grid", "class": "chart-grid"})
    for exponent in Y_EXPONENTS:
        y = svg_map_y(10.0**exponent)
        grid.append(svg_element("line", {"id": f"grid-y-{exponent}", "x1": str(SVG_PLOT_LEFT), "x2": str(SVG_PLOT_RIGHT), "y1": f"{y:.2f}", "y2": f"{y:.2f}", "stroke": "#AAB0B6", "stroke-width": "1.4", "stroke-dasharray": "3 7"}))
    for tick in X_TICKS:
        x = svg_map_x(tick)
        grid.append(svg_element("line", {"id": f"grid-x-{tick}", "x1": f"{x:.2f}", "x2": f"{x:.2f}", "y1": str(SVG_PLOT_TOP), "y2": str(SVG_PLOT_BOTTOM), "stroke": "#AAB0B6", "stroke-width": "1.4", "stroke-dasharray": "3 7"}))
    chart.append(grid)
    axes = svg_element("g", {"id": "chart-axes", "class": "chart-axes"})
    axes.append(svg_element("rect", {"id": "plot-frame", "x": str(SVG_PLOT_LEFT), "y": str(SVG_PLOT_TOP), "width": str(SVG_PLOT_RIGHT-SVG_PLOT_LEFT), "height": str(SVG_PLOT_BOTTOM-SVG_PLOT_TOP), "fill": "none", "stroke": "#111", "stroke-width": "4.5"}))
    x_axis = svg_element("g", {"id": "x-axis", "class": "axis"})
    for tick in X_TICKS:
        x = svg_map_x(tick)
        x_axis.append(svg_element("text", {"id": f"x-label-{tick}", "x": f"{x:.2f}", "y": str(SVG_X_TICK_Y), "text-anchor": "middle", "font-size": "36", "font-weight": "600"}, str(tick)))
    x_axis.append(svg_element("text", {"id": "x-axis-title", "x": f"{(SVG_PLOT_LEFT+SVG_PLOT_RIGHT)/2:.2f}", "y": str(SVG_X_TITLE_Y), "text-anchor": "middle", "font-size": "38", "font-weight": "700"}, "Year"))
    y_axis = svg_element("g", {"id": "y-axis", "class": "axis"})
    for exponent in Y_EXPONENTS:
        y = svg_map_y(10.0**exponent)
        label = svg_element("text", {"id": f"y-label-{exponent}", "x": str(SVG_PLOT_LEFT-25), "y": f"{y+10:.2f}", "text-anchor": "end", "font-size": "34", "font-weight": "600"})
        label.append(svg_element("tspan", text="10"))
        label.append(svg_element("tspan", {"font-size": "22", "dy": "-15"}, str(exponent)))
        y_axis.append(label)
    axes.extend((x_axis, y_axis))
    chart.append(axes)
    data = svg_element("g", {"id": "chart-series", "class": "chart-series"})
    for key in ordered_series(points):
        series = svg_element("g", {"id": f"series-{key}", "class": "data-series", "data-series": key})
        series.append(svg_element("title", text=SERIES_LABELS[key]))
        for obs in [item for item in visible_marker_observations(points) if series_key(item) == key]:
            device = svg_element("g", {"id": f"device-{key}-{obs.device_id}", "class": "device", "data-device-id": obs.device_id})
            add_svg_marker(device, obs)
            series.append(device)
        data.append(series)
    chart.append(data)
    labels = svg_element("g", {"id": "chart-labels", "class": "chart-labels"})
    add_svg_direct_labels(labels)
    chart.append(labels)
    figure.append(chart)
    notes = svg_element("g", {"id": "chart-notes", "class": "chart-notes"})
    notes.append(svg_element("text", {"x": str(SVG_PLOT_LEFT), "y": "1390", "font-size": "20", "fill": "#333"}, "Historical data: Horowitz et al. and Rupp; modern additions use cited manufacturer and SPEC sources."))
    notes.append(svg_element("text", {"x": str(SVG_PLOT_LEFT), "y": "1423", "font-size": "20", "fill": "#333"}, MARKER_NOTE))
    notes.append(svg_element("text", {"x": str(SVG_PLOT_LEFT), "y": "1456", "font-size": "20", "fill": "#555"}, f"Shipping through {cutoff}. Series-specific units and multipliers; compare trends, not cross-series heights."))
    figure.append(notes)
    root.append(figure)
    for element in root.iter():
        if element.tag.endswith("text"):
            element.set("font-family", FONT_STACK)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def font_path(*, bold: bool = False) -> Path:
    path = Path(reportlab.__file__).resolve().parent / "fonts" / ("VeraBd.ttf" if bold else "Vera.ttf")
    if not path.is_file():
        raise RuntimeError("bundled Bitstream Vera font is missing")
    return path


def draw_png_marker(draw: ImageDraw.ImageDraw, obs: Any) -> None:
    key = series_key(obs)
    colour = SERIES_COLOURS[key]
    rgba = tuple(int(colour[index:index+2], 16) for index in (1, 3, 5)) + (int(255*POINT_OPACITY),)
    fill = rgba if obs.filled else (255, 255, 255, 255)
    x, y = map_x(obs.release_year), map_y(display_value(obs))
    radius = marker_radius(key)
    shape = SERIES_SHAPES[key]
    if shape == "circle":
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fill, outline=rgba, width=3 if key in MULTICORE_SERIES else 2)
    else:
        polygon = marker_points(shape, x, y, radius)
        draw.polygon(polygon, fill=fill)
        draw.line(polygon + [polygon[0]], fill=rgba, width=3 if key in MULTICORE_SERIES else 2, joint="curve")


def render_png(path: Path, observations: list[Any], cutoff: str) -> None:
    image = Image.new("RGBA", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image, "RGBA")
    regular14 = ImageFont.truetype(str(font_path()), 14)
    regular22 = ImageFont.truetype(str(font_path()), 22)
    regular24 = ImageFont.truetype(str(font_path()), 24)
    bold21 = ImageFont.truetype(str(font_path(bold=True)), 21)
    bold34 = ImageFont.truetype(str(font_path(bold=True)), 34)
    title = "55 Years of CPU and GPU Compute Trends"
    title_box = draw.textbbox((0, 0), title, font=bold34)
    draw.text(((PLOT_LEFT+PLOT_RIGHT-title_box[2])/2, 46), title, font=bold34, fill="#111")
    def dashed_line(x1: float, y1: float, x2: float, y2: float) -> None:
        length = math.hypot(x2-x1, y2-y1)
        if not length:
            return
        dx, dy = (x2-x1)/length, (y2-y1)/length
        cursor = 0.0
        while cursor < length:
            end = min(cursor+2, length)
            draw.line((x1+dx*cursor, y1+dy*cursor, x1+dx*end, y1+dy*end), fill="#AAB0B6", width=1)
            cursor += 7
    for exponent in Y_EXPONENTS:
        y = map_y(10.0**exponent)
        dashed_line(PLOT_LEFT, y, PLOT_RIGHT, y)
        exp = str(exponent)
        base_width = draw.textlength("10", font=regular22)
        exp_width = draw.textlength(exp, font=regular14)
        x = PLOT_LEFT - 18 - base_width - exp_width
        draw.text((x, y+7), "10", font=regular22, fill="#111", anchor="ls")
        draw.text((x+base_width, y-3), exp, font=regular14, fill="#111", anchor="ls")
    for tick in X_TICKS:
        x = map_x(tick)
        dashed_line(x, PLOT_TOP, x, PLOT_BOTTOM)
        label = str(tick)
        box = draw.textbbox((0, 0), label, font=regular22)
        draw.text((x-box[2]/2, PNG_X_TICK_Y), label, font=regular22, fill="#111")
    draw.rectangle((PLOT_LEFT, PLOT_TOP, PLOT_RIGHT, PLOT_BOTTOM), outline="#111", width=3)
    points = eligible(observations)
    markers = visible_marker_observations(points)
    for obs in markers:
        if series_key(obs) in MULTICORE_SERIES:
            continue
        draw_png_marker(draw, obs)
    for obs in markers:
        if series_key(obs) not in MULTICORE_SERIES:
            continue
        draw_png_marker(draw, obs)
    colours = {**SERIES_COLOURS, "fp32": SERIES_COLOURS["fp32-discrete-gpu"]}
    for identifier, lines, value in DIRECT_LABELS:
        y = map_y(value)
        for index, line in enumerate(lines):
            draw.text((SERIES_LABEL_X, y-17+index*26), line, font=bold21, fill=colours[identifier])
    axis_title = "Year"
    box = draw.textbbox((0, 0), axis_title, font=regular24)
    draw.text(((PLOT_LEFT+PLOT_RIGHT-box[2])/2, PNG_X_TITLE_Y), axis_title, font=regular24, fill="#111")
    draw.text((PLOT_LEFT, PNG_NOTES_Y[0]), "Historical data: Horowitz et al. and Rupp; modern additions use cited manufacturer and SPEC sources.", font=regular14, fill="#333")
    draw.text((PLOT_LEFT, PNG_NOTES_Y[1]), MARKER_NOTE, font=regular14, fill="#333")
    draw.text((PLOT_LEFT, PNG_NOTES_Y[2]), f"Shipping through {cutoff}. {SINGLE_CORE_NOTE} GPU-only FP32 is peak capacity.", font=regular14, fill="#555")
    draw.text((PLOT_LEFT, PNG_NOTES_Y[3]), MULTICORE_NOTE, font=regular14, fill="#555")
    draw.text((PLOT_LEFT, PNG_NOTES_Y[4]), DISPLAY_NOTE, font=regular14, fill="#555")
    image.convert("RGB").save(path, format="PNG", optimize=True)


def pdf_text(c: canvas.Canvas, x: float, y: float, text: str, size: float, colour: str = "#111111", *, bold: bool = False) -> None:
    c.setFont("VeraBold" if bold else "Vera", size)
    c.setFillColor(colour)
    c.drawString(x, HEIGHT-y, text)


def draw_pdf_marker(c: canvas.Canvas, obs: Any) -> None:
    key = series_key(obs)
    colour = SERIES_COLOURS[key]
    x, y = map_x(obs.release_year), HEIGHT-map_y(display_value(obs))
    radius = marker_radius(key)
    c.setStrokeColor(colour); c.setLineWidth(3 if key in MULTICORE_SERIES else 2); c.setStrokeAlpha(POINT_OPACITY)
    if obs.filled:
        c.setFillColor(colour); c.setFillAlpha(POINT_OPACITY)
    else:
        c.setFillColorRGB(1, 1, 1); c.setFillAlpha(1)
    shape = SERIES_SHAPES[key]
    if shape == "circle":
        c.circle(x, y, radius, stroke=1, fill=1)
    else:
        points = [(px, HEIGHT-py) for px, py in marker_points(shape, x, HEIGHT-y, radius)]
        path = c.beginPath(); path.moveTo(*points[0])
        for point in points[1:]: path.lineTo(*point)
        path.close(); c.drawPath(path, stroke=1, fill=1)
    c.setFillAlpha(1); c.setStrokeAlpha(1)


def render_pdf_raw(path: Path, observations: list[Any], cutoff: str) -> None:
    points = eligible(observations)
    markers = visible_marker_observations(points)
    pdfmetrics.registerFont(TTFont("Vera", str(font_path())))
    pdfmetrics.registerFont(TTFont("VeraBold", str(font_path(bold=True))))
    c = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT), pageCompression=0)
    c.setTitle("55 Years of CPU and GPU Compute Trends")
    c.setAuthor("microprocessor-trend-data")
    title = "55 Years of CPU and GPU Compute Trends"
    c.setFont("VeraBold", 34)
    pdf_text(c, (PLOT_LEFT+PLOT_RIGHT-c.stringWidth(title, "VeraBold", 34))/2, 82, title, 34, bold=True)
    c._code.append("%OC_BEGIN chart")
    c.setDash(1, 5)
    for exponent in Y_EXPONENTS:
        y = map_y(10.0**exponent)
        c.setStrokeColor("#AAB0B6"); c.setLineWidth(1); c.line(PLOT_LEFT, HEIGHT-y, PLOT_RIGHT, HEIGHT-y)
        exp = str(exponent)
        base_width = c.stringWidth("10", "Vera", 22)
        exp_width = c.stringWidth(exp, "Vera", 14)
        x = PLOT_LEFT-18-base_width-exp_width
        pdf_text(c, x, y+7, "10", 22)
        pdf_text(c, x+base_width, y-3, exp, 14)
    for tick in X_TICKS:
        x = map_x(tick); c.setStrokeColor("#AAB0B6"); c.line(x, HEIGHT-PLOT_TOP, x, HEIGHT-PLOT_BOTTOM)
        pdf_text(c, x-25, X_TICK_Y, str(tick), 22)
    c.setDash(); c.setStrokeColor("#111111"); c.setLineWidth(3); c.rect(PLOT_LEFT, HEIGHT-PLOT_BOTTOM, PLOT_RIGHT-PLOT_LEFT, PLOT_BOTTOM-PLOT_TOP, stroke=1, fill=0)
    pdf_text(c, (PLOT_LEFT+PLOT_RIGHT-c.stringWidth("Year", "Vera", 24))/2, X_TITLE_Y, "Year", 24)
    mcid = 0
    for key in ordered_series(points):
        c._code.append(f"%OC_BEGIN series-{key}")
        for obs in [item for item in markers if series_key(item) == key]:
            c._code.append(f"/Figure << /MCID {mcid} /ID ({obs.observation_id}) >> BDC")
            draw_pdf_marker(c, obs)
            c._code.append("EMC"); mcid += 1
        c._code.append("%OC_END")
    colours = {**SERIES_COLOURS, "fp32": SERIES_COLOURS["fp32-discrete-gpu"]}
    for identifier, lines, value in DIRECT_LABELS:
        y = map_y(value)
        for index, line in enumerate(lines):
            pdf_text(c, SERIES_LABEL_X, y+index*26, line, 21, colours[identifier], bold=True)
    c._code.append("%OC_END")
    pdf_text(c, PLOT_LEFT, NOTES_Y[0], "Historical data: Horowitz et al. and Rupp; modern additions use cited manufacturer and SPEC sources.", 14, "#333333")
    pdf_text(c, PLOT_LEFT, NOTES_Y[1], MARKER_NOTE, 14, "#333333")
    pdf_text(c, PLOT_LEFT, NOTES_Y[2], f"Shipping through {cutoff}. {SINGLE_CORE_NOTE} GPU-only FP32 is peak capacity.", 14, "#555555")
    pdf_text(c, PLOT_LEFT, NOTES_Y[3], MULTICORE_NOTE, 14, "#555555")
    pdf_text(c, PLOT_LEFT, NOTES_Y[4], DISPLAY_NOTE, 14, "#555555")
    c.showPage(); c.save()


def structure_pdf(raw_path: Path, output_path: Path, observations: list[Any]) -> None:
    points = eligible(observations)
    markers = visible_marker_observations(points)
    reader = PdfReader(raw_path); writer = PdfWriter(); writer.clone_document_from_reader(reader); writer.pdf_header = "%PDF-1.7"
    page = writer.pages[0]
    content = page.get_contents().get_data().decode("latin-1")
    content = re.sub(r"BT\s+/\S+\s+[0-9.]+\s+Tf\s+[0-9.]+\s+TL\s+ET\s*", "", content)
    resources = page[NameObject("/Resources")].get_object()
    fonts = resources[NameObject("/Font")].get_object()
    for name in list(fonts):
        if not re.search(rf"{re.escape(str(name))}\s+[0-9.]+\s+Tf\b", content): del fonts[name]
    properties = DictionaryObject(); resources[NameObject("/Properties")] = properties
    layers: dict[str, Any] = {}
    chart_ref = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/OCG"), NameObject("/Name"): TextStringObject("Overlaid chart") }))
    layers["chart"] = chart_ref
    order = ArrayObject([TextStringObject("Overlaid chart"), chart_ref])
    for key in ordered_series(points):
        ref = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/OCG"), NameObject("/Name"): TextStringObject(SERIES_LABELS[key])}))
        layers[f"series-{key}"] = ref; order.append(ref)
    for index, (identifier, ref) in enumerate(layers.items(), 1):
        prop = NameObject(f"/Layer{index}"); properties[prop] = ref
        content = content.replace(f"%OC_BEGIN {identifier}\n", f"/OC {prop} BDC\n")
    content = content.replace("%OC_END\n", "EMC\n")
    if "%OC_" in content: raise RuntimeError("unresolved PDF layer marker")
    stream = DecodedStreamObject(); stream.set_data(content.encode("latin-1")); page[NameObject("/Contents")] = writer._add_object(stream)
    all_layers = ArrayObject(list(layers.values()))
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({NameObject("/OCGs"): all_layers, NameObject("/D"): DictionaryObject({NameObject("/Order"): order, NameObject("/ON"): all_layers})})
    point_refs: list[Any] = []; series_refs: list[Any] = []; mcid = 0
    for key in ordered_series(points):
        figures: list[Any] = []
        for obs in [item for item in markers if series_key(item) == key]:
            raw = f"; raw {obs.raw_benchmark_suite} {obs.raw_benchmark_value}" if obs.raw_benchmark_suite else ""
            figure = DictionaryObject({NameObject("/Type"): NameObject("/StructElem"), NameObject("/S"): NameObject("/Figure"), NameObject("/T"): TextStringObject(obs.observation_id), NameObject("/Alt"): TextStringObject(f"{obs.device_name}: {obs.value:g} {obs.unit}; {obs.value_kind}, {obs.confidence}{raw}"), NameObject("/Pg"): page.indirect_reference, NameObject("/K"): NumberObject(mcid)})
            ref = writer._add_object(figure); figures.append(ref); point_refs.append(ref); mcid += 1
        section = DictionaryObject({NameObject("/Type"): NameObject("/StructElem"), NameObject("/S"): NameObject("/Sect"), NameObject("/T"): TextStringObject(SERIES_LABELS[key]), NameObject("/K"): ArrayObject(figures)})
        section_ref = writer._add_object(section)
        for ref in figures: ref.get_object()[NameObject("/P")] = section_ref
        series_refs.append(section_ref)
    chart_section = DictionaryObject({NameObject("/Type"): NameObject("/StructElem"), NameObject("/S"): NameObject("/Sect"), NameObject("/T"): TextStringObject("Overlaid chart"), NameObject("/K"): ArrayObject(series_refs)})
    chart_struct_ref = writer._add_object(chart_section)
    for ref in series_refs: ref.get_object()[NameObject("/P")] = chart_struct_ref
    parent_tree = writer._add_object(DictionaryObject({NameObject("/Nums"): ArrayObject([NumberObject(0), ArrayObject(point_refs)])}))
    struct_root = DictionaryObject({NameObject("/Type"): NameObject("/StructTreeRoot"), NameObject("/K"): ArrayObject([chart_struct_ref]), NameObject("/ParentTree"): parent_tree, NameObject("/ParentTreeNextKey"): NumberObject(1)})
    struct_ref = writer._add_object(struct_root); chart_struct_ref.get_object()[NameObject("/P")] = struct_ref
    page[NameObject("/StructParents")] = NumberObject(0)
    writer._root_object[NameObject("/StructTreeRoot")] = struct_ref
    writer._root_object[NameObject("/MarkInfo")] = DictionaryObject({NameObject("/Marked"): BooleanObject(True)})
    writer._root_object[NameObject("/Lang")] = TextStringObject("en-GB")
    with output_path.open("wb") as stream_out: writer.write(stream_out)


def ps_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def ps_colour(colour: str, lighten: bool = False) -> str:
    rgb = [int(colour[index:index+2], 16)/255 for index in (1, 3, 5)]
    if lighten: rgb = [1-(1-value)*POINT_OPACITY for value in rgb]
    return " ".join(f"{value:.4f}" for value in rgb)


def eps_marker(obs: Any) -> list[str]:
    key = series_key(obs); colour = SERIES_COLOURS[key]
    x, y = map_x(obs.release_year), HEIGHT-map_y(display_value(obs))
    radius = marker_radius(key)
    lines = ["gsave", f"{ps_colour(colour, obs.filled)} setrgbcolor", "3 setlinewidth" if key in MULTICORE_SERIES else "2 setlinewidth", "newpath"]
    shape = SERIES_SHAPES[key]
    if shape == "circle": lines.append(f"{x:.2f} {y:.2f} {radius} 0 360 arc closepath")
    else:
        points = [(px, HEIGHT-py) for px, py in marker_points(shape, x, HEIGHT-y, radius)]
        lines.append(f"{points[0][0]:.2f} {points[0][1]:.2f} moveto")
        lines.extend(f"{px:.2f} {py:.2f} lineto" for px, py in points[1:]); lines.append("closepath")
    if obs.filled: lines.extend(["gsave fill grestore", f"{ps_colour(colour)} setrgbcolor", "stroke"])
    else: lines.extend(["1 1 1 setrgbcolor gsave fill grestore", f"{ps_colour(colour)} setrgbcolor", "stroke"])
    lines.append("grestore"); return lines


def render_eps(path: Path, observations: list[Any], cutoff: str) -> None:
    points = eligible(observations)
    markers = visible_marker_observations(points)
    lines = ["%!PS-Adobe-3.0 EPSF-3.0", f"%%BoundingBox: 0 0 {WIDTH} {HEIGHT}", "%%Title: 55 Years of CPU and GPU Compute Trends", "%%Creator: generate_compute_trends.py", "%%DocumentFonts: Helvetica Helvetica-Bold", "%%Pages: 1", "%%EndComments", "%%BeginObject: figure", "1 1 1 setrgbcolor", f"0 0 {WIDTH} {HEIGHT} rectfill", "/Helvetica-Bold findfont 34 scalefont setfont", "0 0 0 setrgbcolor", f"{(PLOT_LEFT+PLOT_RIGHT)/2:.2f} {HEIGHT-82} moveto (55 Years of CPU and GPU Compute Trends) dup stringwidth pop -2 div 0 rmoveto show", "%%BeginObject: chart", "%%BeginObject: chart-grid", "[1 5] 0 setdash"]
    for exponent in Y_EXPONENTS:
        y = HEIGHT-map_y(10.0**exponent); lines.extend(["0.67 0.69 0.71 setrgbcolor", "1 setlinewidth", f"newpath {PLOT_LEFT} {y:.2f} moveto {PLOT_RIGHT} {y:.2f} lineto stroke"])
    for tick in X_TICKS:
        x = map_x(tick); lines.extend(["0.67 0.69 0.71 setrgbcolor", f"newpath {x:.2f} {HEIGHT-PLOT_TOP} moveto {x:.2f} {HEIGHT-PLOT_BOTTOM} lineto stroke"])
    lines.extend(["[] 0 setdash", "%%EndObject", "%%BeginObject: chart-axes", "0.07 0.07 0.07 setrgbcolor", "3 setlinewidth", f"newpath {PLOT_LEFT} {HEIGHT-PLOT_BOTTOM} moveto {PLOT_RIGHT} {HEIGHT-PLOT_BOTTOM} lineto {PLOT_RIGHT} {HEIGHT-PLOT_TOP} lineto {PLOT_LEFT} {HEIGHT-PLOT_TOP} lineto closepath stroke", "/Helvetica findfont 22 scalefont setfont", "/showLogTick { gsave /exptxt exch def /baseline exch def /right exch def /Helvetica findfont 22 scalefont setfont (10) stringwidth pop /basewidth exch def /Helvetica findfont 14 scalefont setfont exptxt stringwidth pop /expwidth exch def right basewidth expwidth add sub baseline moveto /Helvetica findfont 22 scalefont setfont (10) show 0 10 rmoveto /Helvetica findfont 14 scalefont setfont exptxt show grestore } bind def"])
    for exponent in Y_EXPONENTS:
        lines.append(f"{PLOT_LEFT-18} {HEIGHT-map_y(10.0**exponent)-7:.2f} ({exponent}) showLogTick")
    for tick in X_TICKS: lines.append(f"{map_x(tick)-25:.2f} {HEIGHT-X_TICK_Y} moveto ({tick}) show")
    lines.extend(["/Helvetica findfont 24 scalefont setfont", f"{(PLOT_LEFT+PLOT_RIGHT)/2:.2f} {HEIGHT-X_TITLE_Y} moveto (Year) dup stringwidth pop -2 div 0 rmoveto show", "%%EndObject", "%%BeginObject: chart-series"])
    for key in ordered_series(points):
        lines.append(f"%%BeginObject: series-{key}")
        for obs in [item for item in markers if series_key(item) == key]:
            lines.extend([f"%%BeginObject: device-{key}-{obs.device_id}", f"%%BeginObject: {obs.observation_id}", f"%%PointData: device={obs.device_id} metric={obs.metric} year={obs.release_year:.6f} value={obs.value:.12g} display_value={display_value(obs):.12g} confidence={obs.confidence} raw_suite={obs.raw_benchmark_suite or '-'} raw_value={obs.raw_benchmark_value or '-'}", *eps_marker(obs), "%%EndObject", "%%EndObject"])
        lines.append("%%EndObject")
    lines.extend(["%%EndObject", "%%BeginObject: chart-labels", "/Helvetica-Bold findfont 21 scalefont setfont"])
    colours = {**SERIES_COLOURS, "fp32": SERIES_COLOURS["fp32-discrete-gpu"]}
    for identifier, label_lines, value in DIRECT_LABELS:
        lines.append(f"{ps_colour(colours[identifier])} setrgbcolor")
        y = HEIGHT-map_y(value)
        for index, line in enumerate(label_lines): lines.append(f"{SERIES_LABEL_X} {y-index*26:.2f} moveto ({ps_escape(line)}) show")
    lines.extend(["%%EndObject", "%%EndObject", "%%BeginObject: chart-notes", "/Helvetica findfont 14 scalefont setfont", "0.2 0.2 0.2 setrgbcolor", f"{PLOT_LEFT} {HEIGHT-NOTES_Y[0]} moveto (Historical data: Horowitz et al. and Rupp; modern additions use cited manufacturer and SPEC sources.) show", f"{PLOT_LEFT} {HEIGHT-NOTES_Y[1]} moveto ({ps_escape(MARKER_NOTE)}) show", f"{PLOT_LEFT} {HEIGHT-NOTES_Y[2]} moveto (Shipping through {ps_escape(cutoff)}. {ps_escape(SINGLE_CORE_NOTE)} GPU-only FP32 is peak capacity.) show", f"{PLOT_LEFT} {HEIGHT-NOTES_Y[3]} moveto ({ps_escape(MULTICORE_NOTE)}) show", f"{PLOT_LEFT} {HEIGHT-NOTES_Y[4]} moveto ({ps_escape(DISPLAY_NOTE)}) show", "%%EndObject", "%%EndObject", "showpage", "%%EOF"])
    path.write_text("\n".join(lines)+"\n", encoding="latin-1")


def verify_svg(path: Path, expected: list[Any]) -> None:
    root = ET.parse(path).getroot()
    if (root.get("width"), root.get("height"), root.get("viewBox")) != (
        str(SVG_WIDTH), str(SVG_HEIGHT), f"0 0 {SVG_WIDTH} {SVG_HEIGHT}"
    ):
        raise RuntimeError("SVG display layout dimensions changed")
    if any(element.tag == f"{{{SVG_NS}}}image" for element in root.iter()): raise RuntimeError("SVG contains a raster image")
    groups = list(root.iter(f"{{{SVG_NS}}}g")); ids = [group.get("id", "") for group in groups]
    if any(not identifier for identifier in ids) or len(ids) != len(set(ids)): raise RuntimeError("SVG has anonymous or duplicate group IDs")
    if "series-fp32-cpu" in ids or any(obs.metric == "fp32_dense_peak_gflops" and not obs.gpu_component for obs in expected):
        raise RuntimeError("CPU FP32 must not appear in the chart")
    point_ids = [group.get("id") for group in groups if group.get("class") == "data-point"]
    markers = visible_marker_observations(expected)
    if sorted(point_ids) != sorted(obs.observation_id for obs in markers): raise RuntimeError("SVG point objects do not match visible chart markers")
    by_id = {group.get("id"): group for group in groups if group.get("class") == "data-point"}
    for obs in markers:
        point = by_id[obs.observation_id]
        if (point.get("data-device-id") != obs.device_id
                or point.get("data-metric") != obs.metric
                or point.get("data-series") != series_key(obs)
                or point.get("data-year") != f"{obs.release_year:.6f}"
                or point.get("data-value") != f"{obs.value:.12g}"):
            raise RuntimeError(f"SVG marker is not linked to its device: {obs.observation_id}")
        shape = next((child for child in point if child.tag in (f"{{{SVG_NS}}}path", f"{{{SVG_NS}}}circle")), None)
        if (shape is None or float(shape.get("stroke-width", "0")) < 3.3
                or (shape.get("fill") == "white") == obs.filled):
            raise RuntimeError(f"SVG display marker style changed: {obs.observation_id}")
    frame = root.find(".//*[@id='plot-frame']")
    if (frame is None or frame.get("width") != str(SVG_PLOT_RIGHT-SVG_PLOT_LEFT)
            or float(frame.get("stroke-width", "0")) < 4):
        raise RuntimeError("SVG display plot frame is missing or too light")
    cores = [obs for obs in expected if series_key(obs) == "core-count"]
    if cores:
        series = root.find(".//*[@id='series-core-count']")
        if (series is None
                or len(series.findall(f".//{{{SVG_NS}}}g[@class='data-point']")) != len(cores)
                or series.find(f"{{{SVG_NS}}}polyline") is not None):
            raise RuntimeError("SVG physical cores must be separate point groups without a line")
    for key in MULTICORE_SERIES:
        multicore = [obs for obs in expected if series_key(obs) == key]
        if multicore:
            series = root.find(f".//*[@id='series-{key}']")
            if series is None or len(series.findall(f".//{{{SVG_NS}}}g[@class='data-point']")) != len(multicore):
                raise RuntimeError(f"SVG {key} is missing measured point groups")
            if series.find(f".//*[@id='{key}-vendor-guides']") is not None or series.find(f"{{{SVG_NS}}}polyline") is not None:
                raise RuntimeError(f"SVG {key} must be scatter-only")
    for identifier in ("chart-grid", "chart-axes", "chart-labels", "chart-series", "x-axis", "y-axis"):
        if root.find(f".//*[@id='{identifier}']") is None: raise RuntimeError(f"SVG missing {identifier}")
    for exponent in Y_EXPONENTS:
        label = root.find(f".//*[@id='y-label-{exponent}']")
        spans = list(label) if label is not None else []
        if len(spans) != 2 or spans[0].text != "10" or spans[1].text != str(exponent):
            raise RuntimeError("SVG log ticks must use superscript exponents")
    serialized = path.read_text(encoding="utf-8").lower()
    if any(token in serialized for token in ("@font-face", "data:font", "<font")): raise RuntimeError("SVG embeds a font")


def count_pdf_images(reader: PdfReader) -> int:
    count = 0
    for page in reader.pages:
        resources = page.get("/Resources", {}).get_object(); xobjects = resources.get("/XObject", {})
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        for ref in xobjects.values():
            if str(ref.get_object().get("/Subtype")) == "/Image": count += 1
    return count


def verify_pdf(path: Path, expected: list[Any]) -> None:
    reader = PdfReader(path); root = reader.trailer["/Root"]
    if len(reader.pages) != 1 or count_pdf_images(reader): raise RuntimeError("PDF must be one vector-only page")
    if root.get("/OCProperties") is None or root.get("/StructTreeRoot") is None: raise RuntimeError("PDF layers or tagged structure are missing")
    content = reader.pages[0].get_contents().get_data().decode("latin-1")
    if content.count("/Figure << /MCID") != len(visible_marker_observations(expected)): raise RuntimeError("PDF marked point count mismatch")
    if len(root["/OCProperties"].get_object()["/OCGs"]) != 1+len(ordered_series(expected)): raise RuntimeError("PDF series layers are incomplete")
    layer_names = {str(ref.get_object()["/Name"]) for ref in root["/OCProperties"].get_object()["/OCGs"]}
    if any("FP32" in name and "CPU" in name for name in layer_names): raise RuntimeError("PDF contains a CPU FP32 layer")
    if any(series_key(obs) in MULTICORE_SERIES for obs in expected):
        if not all(SERIES_LABELS[key] in layer_names for key in MULTICORE_SERIES if any(series_key(obs) == key for obs in expected)):
            raise RuntimeError("PDF throughput index layer is missing")
    if any(series_key(obs) == "core-count" for obs in expected):
        if SERIES_LABELS["core-count"] not in layer_names:
            raise RuntimeError("PDF physical-core layer is missing")
    fonts = reader.pages[0]["/Resources"]["/Font"].get_object()
    for ref in fonts.values():
        font = ref.get_object(); descriptor = font.get("/FontDescriptor")
        descriptor = descriptor.get_object() if hasattr(descriptor, "get_object") else descriptor
        if descriptor is None or not any(descriptor.get(name) for name in ("/FontFile", "/FontFile2", "/FontFile3")): raise RuntimeError("PDF font is not embedded")


def verify_eps(path: Path, expected: list[Any]) -> None:
    text = path.read_text(encoding="latin-1"); executable = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("%"))
    if re.search(r"\b(?:image|colorimage|imagemask)\b", executable, re.I): raise RuntimeError("EPS contains a raster operator")
    if text.count("%%BeginObject:") != text.count("%%EndObject"): raise RuntimeError("EPS object groups are unbalanced")
    if text.count("%%PointData:") != len(visible_marker_observations(expected)): raise RuntimeError("EPS point count mismatch")
    if "%%BeginObject: series-fp32-cpu" in text: raise RuntimeError("EPS contains a CPU FP32 series")
    if "-vendor-guides" in text or "%%BeginObject: annotation-multicore-onset" in text:
        raise RuntimeError("EPS contains an unwanted connector or IBM POWER4 callout")
    for key in MULTICORE_SERIES:
        multicore = [obs for obs in expected if series_key(obs) == key]
        if multicore and (text.count(f"%%BeginObject: series-{key}\n") != 1 or text.count("metric=cpu_multicore_index ") != len(multicore)):
            raise RuntimeError(f"EPS {key} is missing indexed point objects")
    if any(series_key(obs) == "core-count" for obs in expected):
        cores = [obs for obs in expected if series_key(obs) == "core-count"]
        if (text.count("%%BeginObject: series-core-count\n") != 1
                or text.count("metric=cpu_physical_cores ") != len(cores)
                or "%%BeginObject: core-frontier-step\n" in text):
            raise RuntimeError("EPS physical cores must be separate point objects without a line")


def verify_png(path: Path) -> None:
    with Image.open(path) as image:
        if image.size != (WIDTH, HEIGHT) or image.format != "PNG": raise RuntimeError("PNG dimensions or format are incorrect")


def metric_counts(observations: Iterable[Any]) -> Counter[str]:
    return Counter(obs.metric for obs in eligible(observations))
