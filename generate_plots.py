#!/usr/bin/env python3
"""Generate editable PNG, SVG, PDF, and EPS versions of every chart edition."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    ContentStream,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


ROOT = Path(__file__).resolve().parent
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
SVG_FONT_STACK = "Helvetica, Arial, Liberation Sans, sans-serif"
POINT_OPACITY = 0.82
POINT_RGB = {
    "cores": "000000",
    "frequency": "008800",
    "specint": "0000BB",
    "transistors": "CC6600",
    "watts": "BB0000",
}

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


@dataclass(frozen=True)
class Series:
    key: str
    label: str
    filename: str
    eps_marker: str


@dataclass(frozen=True)
class Edition:
    years: int
    width: int
    height: int
    x_range_max: float
    y_range_max: float
    label_x: float
    update_through: int
    margins: tuple[float, float, float, float]
    expected_counts: tuple[int, int, int, int, int]

    @property
    def key(self) -> str:
        return str(self.years)

    @property
    def directory(self) -> Path:
        return ROOT / f"{self.years}yrs"

    @property
    def basename(self) -> str:
        return f"{self.years}-years-processor-trend"

    @property
    def title(self) -> str:
        return f"{self.years} Years of Microprocessor Trend Data"

    @property
    def update_attribution(self) -> str:
        return f"New plot and data collected for 2010-{self.update_through} by K. Rupp"


@dataclass(frozen=True)
class Point:
    series: Series
    index: int
    source_line: int
    year: str
    value: str
    comment: str

    @property
    def identifier(self) -> str:
        return f"{self.series.key}-point-{self.index:03d}"

    @property
    def description(self) -> str:
        text = f"{self.series.label}: year {self.year}, value {self.value}"
        return f"{text} ({self.comment})" if self.comment else text


SERIES = (
    Series("cores", "Logical cores", "cores.dat", "DiaF"),
    Series("frequency", "Frequency (MHz)", "frequency.dat", "BoxF"),
    Series("specint", "Single-thread performance", "specint.dat", "CircleF"),
    Series("transistors", "Transistors (thousands)", "transistors.dat", "TriUF"),
    Series("watts", "Typical power (watts)", "watts.dat", "TriDF"),
)

EDITIONS = {
    "40": Edition(
        years=40,
        width=1578,
        height=1006,
        x_range_max=2020,
        y_range_max=1e7,
        label_x=2020.5,
        update_through=2015,
        margins=(0.052, 0.797, 0.185, 0.923),
        expected_counts=(73, 84, 66, 87, 85),
    ),
    "42": Edition(
        years=42,
        width=1578,
        height=1000,
        x_range_max=2020,
        y_range_max=3e7,
        label_x=2020.5,
        update_through=2017,
        margins=(0.052, 0.797, 0.182, 0.923),
        expected_counts=(79, 90, 69, 90, 90),
    ),
    "48": Edition(
        years=48,
        width=1586,
        height=1000,
        x_range_max=2020,
        y_range_max=5e7,
        label_x=2020.5,
        update_through=2019,
        margins=(0.052, 0.797, 0.177, 0.923),
        expected_counts=(83, 94, 71, 94, 94),
    ),
    "50": Edition(
        years=50,
        width=1600,
        height=1117,
        x_range_max=2022.5,
        y_range_max=7e7,
        label_x=2023,
        update_through=2021,
        margins=(0.080, 0.800, 0.180, 0.830),
        expected_counts=(90, 101, 74, 99, 101),
    ),
}
FORMATS = ("png", "svg", "pdf", "eps")

DIRECT_LAYOUT = {"png": 1, "svg": 1, "pdf": 1, "eps": 0}
MARKER_SCALE = {"png": 1.0, "svg": 1.27, "pdf": 0.44, "eps": 0.9}
MARKER_SCALES = {
    "png": (1.0, 1.0, 1.0, 1.0, 1.0),
    "svg": (1.27, 1.8, 1.8, 1.27, 1.27),
    "pdf": (0.44, 0.44, 0.44, 0.44, 0.44),
    "eps": (0.9, 0.9, 0.9, 0.9, 0.9),
}
ATTRIBUTION_FONT = {"png": ",7", "svg": ",7", "pdf": ",7", "eps": ",8"}
XTIC_Y_OFFSET = {"png": 0.0, "svg": -1.75, "pdf": 0.0, "eps": 0.0}
YTIC_X_OFFSET = {"png": 0.0, "svg": -1.5, "pdf": -0.1, "eps": 0.0}
XLABEL_Y_OFFSET = {"png": 0.13, "svg": -3.43, "pdf": 0.0, "eps": 0.0}
TITLE_Y_OFFSET = {"png": 0.0, "svg": 1.1, "pdf": 0.0, "eps": 0.0}
ATTRIBUTION_Y_OFFSET = {"png": 0.0, "svg": -0.31, "pdf": 0.0, "eps": 0.0}


def terminal_for(fmt: str, edition: Edition) -> str:
    if fmt == "png":
        return (
            f"pngcairo size {edition.width},{edition.height} enhanced font 'Helvetica,12' "
            "fontscale 2.5 linewidth 2.777778 pointscale 2.25"
        )
    if fmt == "svg":
        return (
            f"svg size {edition.width},{edition.height} enhanced dynamic font 'Helvetica,12' "
            "fontscale 3.333333 linewidth 2.777778"
        )
    if fmt == "pdf":
        width = edition.width / 400
        height = edition.height / 400
        return (
            "pdfcairo color enhanced font 'Helvetica,12' fontscale 0.45 "
            f"size {width:.6f}in,{height:.6f}in"
        )
    if fmt == "eps":
        return "postscript eps color enhanced"
    raise ValueError(f"unsupported format: {fmt}")


def parse_points(edition: Edition) -> dict[str, list[Point]]:
    parsed: dict[str, list[Point]] = {}
    for series in SERIES:
        points: list[Point] = []
        path = edition.directory / series.filename
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            data, _, comment = raw_line.partition("#")
            fields = data.split()
            if not fields:
                continue
            if len(fields) != 2:
                raise ValueError(f"{path}:{line_number}: expected two columns")
            try:
                float(fields[0])
                float(fields[1])
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: non-numeric data") from exc
            points.append(
                Point(
                    series=series,
                    index=len(points) + 1,
                    source_line=line_number,
                    year=fields[0],
                    value=fields[1],
                    comment=comment.strip(),
                )
            )
        parsed[series.key] = points
    return parsed


def gnuplot_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_raw(fmt: str, destination: Path, edition: Edition) -> None:
    left, right, bottom, top = edition.margins
    expression = ";".join(
        (
            f"OUTPUT_TERMINAL={gnuplot_quote(terminal_for(fmt, edition))}",
            f"OUTPUT_FILE={gnuplot_quote(str(destination))}",
            f"PLOT_TITLE={gnuplot_quote(edition.title)}",
            f"UPDATE_ATTRIBUTION={gnuplot_quote(edition.update_attribution)}",
            f"X_RANGE_MAX={edition.x_range_max}",
            f"Y_RANGE_MAX={edition.y_range_max}",
            f"LABEL_X={edition.label_x}",
            f"DIRECT_LAYOUT={DIRECT_LAYOUT[fmt]}",
            f"LEFT_MARGIN={left}",
            f"RIGHT_MARGIN={right}",
            f"BOTTOM_MARGIN={bottom}",
            f"TOP_MARGIN={top}",
            f"MARKER_SCALE={MARKER_SCALE[fmt]}",
            f"ATTRIBUTION_FONT={gnuplot_quote(ATTRIBUTION_FONT[fmt])}",
            f"XTIC_Y_OFFSET={XTIC_Y_OFFSET[fmt]}",
            f"YTIC_X_OFFSET={YTIC_X_OFFSET[fmt]}",
            f"XLABEL_Y_OFFSET={XLABEL_Y_OFFSET[fmt]}",
            f"TITLE_Y_OFFSET={TITLE_Y_OFFSET[fmt]}",
            f"ATTRIBUTION_Y_OFFSET={ATTRIBUTION_Y_OFFSET[fmt]}",
        )
    )
    marker_scale_expressions = [
        f"MARKER_SCALE_{series.key.upper()}={scale}"
        for series, scale in zip(SERIES, MARKER_SCALES[fmt], strict=True)
    ]
    # PNG transparency must be supplied to the renderer. SVG and PDF receive
    # explicit editable opacity during semantic post-processing; EPS remains
    # opaque because standard PostScript has no alpha channel.
    transparency = round((1.0 - POINT_OPACITY) * 255) if fmt == "png" else 0
    point_color_expressions = [
        f"POINT_COLOR_{series.key.upper()}="
        f"{gnuplot_quote(f'#{transparency:02X}{POINT_RGB[series.key]}')}"
        for series in SERIES
    ]
    expression = ";".join(
        (expression, *marker_scale_expressions, *point_color_expressions)
    )
    subprocess.run(
        ["gnuplot", "-e", expression, str(ROOT / "plot.gnuplot")],
        cwd=edition.directory,
        check=True,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"gnuplot did not create {destination}")


def point_attributes(point: Point) -> dict[str, str]:
    attributes = {
        "id": point.identifier,
        "class": "data-point",
        "data-series": point.series.key,
        "data-index": str(point.index),
        "data-year": point.year,
        "data-value": point.value,
        "data-source-line": str(point.source_line),
        "opacity": f"{POINT_OPACITY:.2f}",
    }
    if point.comment:
        attributes["data-comment"] = point.comment
    return attributes


def svg_group(identifier: str, class_name: str, label: str) -> ET.Element:
    group = ET.Element(
        f"{{{SVG_NS}}}g",
        {"id": identifier, "class": class_name},
    )
    ET.SubElement(group, f"{{{SVG_NS}}}title").text = label
    return group


def inherit_svg_attributes(source: ET.Element, target: ET.Element) -> None:
    """Materialize inherited presentation attributes before moving an element."""

    for name, value in source.attrib.items():
        if name not in {"id", "class"} and not name.startswith("data-"):
            target.attrib.setdefault(name, value)


def svg_text_value(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def flatten_svg_text(source: ET.Element, identifier: str) -> ET.Element:
    texts = list(source.iter(f"{{{SVG_NS}}}text"))
    if len(texts) != 1:
        raise RuntimeError(f"expected one SVG text element in {identifier}, found {len(texts)}")
    text = texts[0]
    inherit_svg_attributes(source, text)
    text.set("id", identifier)
    return text


def structure_svg(
    raw_path: Path,
    output_path: Path,
    points: dict[str, list[Point]],
    edition: Edition,
) -> None:
    tree = ET.parse(raw_path)
    root = tree.getroot()
    plot_groups = [
        element
        for element in root.iter(f"{{{SVG_NS}}}g")
        if re.fullmatch(r"gnuplot_plot_\d+", element.get("id", ""))
    ]
    if len(plot_groups) != len(SERIES):
        raise RuntimeError(f"expected {len(SERIES)} SVG plot groups, found {len(plot_groups)}")

    for series, group in zip(SERIES, plot_groups, strict=True):
        group.set("id", f"series-{series.key}")
        group.set("class", "data-series")
        group.set("data-series", series.key)
        title = group.find(f"{{{SVG_NS}}}title")
        if title is None:
            title = ET.Element(f"{{{SVG_NS}}}title")
            group.insert(0, title)
        title.text = series.label

        parent_map = {child: parent for parent in group.iter() for child in parent}
        uses = [element for element in group.iter(f"{{{SVG_NS}}}use")]
        series_points = points[series.key]
        if len(uses) != len(series_points):
            raise RuntimeError(
                f"{series.key}: expected {len(series_points)} SVG markers, found {len(uses)}"
            )
        for use, point in zip(uses, series_points, strict=True):
            parent = parent_map[use]
            position = list(parent).index(use)
            parent.remove(use)
            wrapper = ET.Element(f"{{{SVG_NS}}}g", point_attributes(point))
            point_title = ET.SubElement(wrapper, f"{{{SVG_NS}}}title")
            point_title.text = point.description
            wrapper.append(use)
            parent.insert(position, wrapper)

        # Gnuplot puts all markers behind one anonymous presentation group.
        # Materialize those inherited styles on every named point group so the
        # series contains only the individually editable, named points.
        point_groups = [
            element for element in group.iter(f"{{{SVG_NS}}}g")
            if element.get("class") == "data-point"
        ]
        for point_group in point_groups:
            marker_parent = next(
                parent
                for parent in group.iter(f"{{{SVG_NS}}}g")
                if point_group in list(parent)
            )
            inherit_svg_attributes(marker_parent, point_group)
        title = group.find(f"{{{SVG_NS}}}title")
        for child in list(group):
            group.remove(child)
        if title is not None:
            group.append(title)
        group.extend(point_groups)

    canvas = root.find(f"{{{SVG_NS}}}g[@id='gnuplot_canvas']")
    if canvas is None:
        raise RuntimeError("SVG is missing the gnuplot canvas")
    children = list(canvas)
    defs = next((child for child in children if child.tag == f"{{{SVG_NS}}}defs"), None)
    background = next((child for child in children if child.tag == f"{{{SVG_NS}}}rect"), None)
    if defs is None or background is None:
        raise RuntimeError("SVG is missing definitions or its canvas rectangle")
    defs.set("id", "chart-definitions")
    background.set("id", "chart-background-rect")

    direct_groups = [child for child in children if child.tag == f"{{{SVG_NS}}}g"]
    grid_sources: list[tuple[ET.Element, ET.Element]] = []
    tick_sources: list[ET.Element] = []
    frame_sources: list[ET.Element] = []
    title_source: ET.Element | None = None
    series_groups = []
    for group in direct_groups:
        if group.get("class") == "data-series":
            series_groups.append(group)
            continue
        grid_path = group.find(f"{{{SVG_NS}}}path[@class='gridline']")
        if grid_path is not None:
            grid_sources.append((group, grid_path))
            continue
        paths = group.findall(f"{{{SVG_NS}}}path")
        text_value = svg_text_value(group)
        if paths and "Z" in paths[0].get("d", ""):
            frame_sources.append(group)
        elif paths and group.find(f".//{{{SVG_NS}}}text") is not None:
            tick_sources.append(group)
        elif text_value == edition.title:
            title_source = group
        elif len(group) != 0:
            raise RuntimeError(f"unclassified SVG canvas group: {text_value!r}")

    if len(grid_sources) != 14 or len(tick_sources) != 14 or len(frame_sources) != 2:
        raise RuntimeError(
            "unexpected SVG scaffolding counts: "
            f"grid={len(grid_sources)} ticks={len(tick_sources)} frames={len(frame_sources)}"
        )
    if title_source is None or len(series_groups) != len(SERIES):
        raise RuntimeError("SVG is missing its title or data-series groups")

    horizontal_grid = svg_group("grid-horizontal", "grid-lines", "Horizontal grid lines")
    vertical_grid = svg_group("grid-vertical", "grid-lines", "Vertical grid lines")
    def is_horizontal_grid(path: ET.Element) -> bool:
        coordinates = re.findall(r"[-+]?\d+(?:\.\d+)?", path.get("d", ""))
        if len(coordinates) < 4:
            raise RuntimeError(f"cannot parse SVG grid path: {path.get('d', '')}")
        x1, y1, x2, y2 = map(float, coordinates[:4])
        return abs(x2 - x1) > abs(y2 - y1)

    y_grid_sources = [item for item in grid_sources if is_horizontal_grid(item[1])]
    x_grid_sources = [item for item in grid_sources if item not in y_grid_sources]
    y_values = tuple(f"1e{exponent}" for exponent in range(8))
    x_values = ("1970", "1980", "1990", "2000", "2010", "2020")
    if len(y_grid_sources) != len(y_values) or len(x_grid_sources) != len(x_values):
        raise RuntimeError("SVG grid orientation could not be determined")
    for (source, path), value in zip(y_grid_sources, y_values, strict=True):
        inherit_svg_attributes(source, path)
        path.set("id", f"grid-y-{value}")
        horizontal_grid.append(path)
    for (source, path), value in zip(x_grid_sources, x_values, strict=True):
        inherit_svg_attributes(source, path)
        path.set("id", f"grid-x-{value}")
        vertical_grid.append(path)
    chart_grid = svg_group("chart-grid", "chart-component", "Chart grid")
    chart_grid.extend((horizontal_grid, vertical_grid))

    y_tick_sources = tick_sources[: len(y_values)]
    x_tick_sources = tick_sources[len(y_values) :]
    if len(x_tick_sources) != len(x_values):
        raise RuntimeError("SVG tick orientation could not be determined")
    y_ticks = svg_group("y-axis-ticks", "axis-ticks", "Y-axis ticks and labels")
    x_ticks = svg_group("x-axis-ticks", "axis-ticks", "X-axis ticks and labels")
    for source, value in zip(y_tick_sources, y_values, strict=True):
        tick = svg_group(f"y-tick-{value}", "axis-tick", f"Y tick 10^{value[2:]}")
        path = source.find(f"{{{SVG_NS}}}path")
        label_source = source.find(f"{{{SVG_NS}}}g")
        if path is None or label_source is None:
            raise RuntimeError(f"SVG y-axis tick {value} is incomplete")
        inherit_svg_attributes(source, path)
        path.set("id", f"y-tick-{value}-mark")
        tick.extend((path, flatten_svg_text(label_source, f"y-tick-{value}-label")))
        y_ticks.append(tick)
    for source, value in zip(x_tick_sources, x_values, strict=True):
        tick = svg_group(f"x-tick-{value}", "axis-tick", f"X tick {value}")
        path = source.find(f"{{{SVG_NS}}}path")
        label_source = source.find(f"{{{SVG_NS}}}g")
        if path is None or label_source is None:
            raise RuntimeError(f"SVG x-axis tick {value} is incomplete")
        inherit_svg_attributes(source, path)
        path.set("id", f"x-tick-{value}-mark")
        tick.extend((path, flatten_svg_text(label_source, f"x-tick-{value}-label")))
        x_ticks.append(tick)

    label_frame = max(frame_sources, key=lambda group: len(list(group.iter(f"{{{SVG_NS}}}text"))))
    axis_frame = min(frame_sources, key=lambda group: len(list(group.iter(f"{{{SVG_NS}}}text"))))
    frame_path = axis_frame.find(f"{{{SVG_NS}}}path")
    underlay_frame_path = label_frame.find(f"{{{SVG_NS}}}path")
    if frame_path is None or underlay_frame_path is None:
        raise RuntimeError("SVG plot frame is missing")
    inherit_svg_attributes(label_frame, underlay_frame_path)
    underlay_frame_path.set("id", "plot-frame-underlay-path")
    inherit_svg_attributes(axis_frame, frame_path)
    frame_path.set("id", "plot-frame-overlay-path")
    plot_frame = svg_group("plot-frame", "axis-frame", "Plot frame")
    plot_frame.extend((underlay_frame_path, frame_path))

    axis_label_sources = [
        group for group in axis_frame.findall(f"{{{SVG_NS}}}g")
        if svg_text_value(group) == "Year"
    ]
    if len(axis_label_sources) != 1:
        raise RuntimeError("SVG x-axis title is missing")
    x_axis_title = svg_group("x-axis-title", "axis-title", "X-axis title")
    x_axis_title.append(flatten_svg_text(axis_label_sources[0], "x-axis-title-text"))
    x_axis = svg_group("x-axis", "axis", "X axis")
    x_axis.extend((x_ticks, x_axis_title))
    y_axis = svg_group("y-axis", "axis", "Y axis")
    y_axis.append(y_ticks)
    chart_axes = svg_group("chart-axes", "chart-component", "Chart axes")
    chart_axes.extend((x_axis, y_axis, plot_frame))

    data_series = svg_group("data-series", "chart-component", "Data series")
    data_series.extend(series_groups)

    series_label_lines = {
        "cores": ("Number of", "Logical Cores"),
        "frequency": ("Frequency (MHz)",),
        "specint": ("Single-Thread", "Performance", "(SpecINT x 103)"),
        "transistors": ("Transistors", "(thousands)"),
        "watts": ("Typical Power", "(Watts)"),
    }
    text_sources = label_frame.findall(f"{{{SVG_NS}}}g")
    text_by_value = {svg_text_value(group): group for group in text_sources}
    series_labels = svg_group("series-labels", "chart-component", "Series labels")
    for series in SERIES:
        label_group = svg_group(
            f"series-label-{series.key}", "series-label", f"{series.label} label"
        )
        for line_number, text_value in enumerate(series_label_lines[series.key], 1):
            source = text_by_value.get(text_value)
            if source is None:
                raise RuntimeError(f"SVG is missing series label line: {text_value}")
            label_group.append(
                flatten_svg_text(source, f"series-label-{series.key}-line-{line_number}")
            )
        series_labels.append(label_group)

    annotations = svg_group("chart-annotations", "chart-component", "Attribution notes")
    annotation_specs = (
        ("Original data up to", "annotation-original-data"),
        ("New plot and data", "annotation-updated-data"),
    )
    for prefix, identifier in annotation_specs:
        matches = [source for value, source in text_by_value.items() if value.startswith(prefix)]
        if len(matches) != 1:
            raise RuntimeError(f"SVG is missing annotation beginning {prefix!r}")
        annotation = svg_group(identifier, "chart-annotation", svg_text_value(matches[0]))
        annotation.append(flatten_svg_text(matches[0], f"{identifier}-text"))
        annotations.append(annotation)

    title_text_source = title_source.find(f"{{{SVG_NS}}}g")
    if title_text_source is None:
        raise RuntimeError("SVG chart title text is missing")
    title_group = svg_group("chart-title", "chart-component", "Chart title")
    title_group.append(flatten_svg_text(title_text_source, "chart-title-text"))
    background_group = svg_group("chart-background", "chart-component", "Chart background")
    background_group.append(background)

    root_title = root.find(f"{{{SVG_NS}}}title")
    root_description = root.find(f"{{{SVG_NS}}}desc")
    if root_title is not None:
        root_title.text = edition.title
    if root_description is not None:
        root_description.text = "Editable vector chart with semantically named groups"
    canvas.set("id", "chart")
    canvas.set("class", "editable-chart")
    for child in list(canvas):
        canvas.remove(child)
    canvas.extend(
        (
            defs,
            background_group,
            chart_grid,
            data_series,
            chart_axes,
            series_labels,
            annotations,
            title_group,
        )
    )

    # Keep SVG text portable without embedding font data. Helvetica preserves
    # the reference rendering where available; the remaining names provide
    # metrically similar, generic fallbacks on other platforms.
    for element in root.iter():
        if "font-family" in element.attrib:
            element.set("font-family", SVG_FONT_STACK)

    if any(element.tag == f"{{{SVG_NS}}}image" for element in root.iter()):
        raise RuntimeError("SVG unexpectedly contains a raster image")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


def pdf_string(value: str) -> TextStringObject:
    return TextStringObject(value)


def structure_pdf(raw_path: Path, output_path: Path, points: dict[str, list[Point]]) -> None:
    reader = PdfReader(raw_path)
    if len(reader.pages) != 1:
        raise RuntimeError("expected a one-page PDF")
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.pdf_header = "%PDF-1.5"
    page = writer.pages[0]
    content = ContentStream(page.get_contents(), writer)

    fill_indices = [
        index
        for index, (_, operator) in enumerate(content.operations)
        if operator in (b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*")
    ]
    all_points = [point for series in SERIES for point in points[series.key]]
    if len(fill_indices) != len(all_points):
        raise RuntimeError(
            f"expected {len(all_points)} PDF marker fills, found {len(fill_indices)}"
        )

    ocg_refs: dict[str, object] = {}
    for series in SERIES:
        ocg = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/OCG"),
                NameObject("/Name"): pdf_string(series.label),
            }
        )
        ocg_refs[series.key] = writer._add_object(ocg)

    resources = page.get("/Resources").get_object()
    ext_gstates = resources.get("/ExtGState")
    if ext_gstates is None:
        ext_gstates = DictionaryObject()
        resources[NameObject("/ExtGState")] = ext_gstates
    else:
        ext_gstates = ext_gstates.get_object()
    point_opacity_name = NameObject("/DataPointOpacity")
    ext_gstates[point_opacity_name] = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/ExtGState"),
                NameObject("/ca"): FloatObject(POINT_OPACITY),
                NameObject("/CA"): FloatObject(POINT_OPACITY),
            }
        )
    )
    properties = resources.get("/Properties")
    if properties is None:
        properties = DictionaryObject()
        resources[NameObject("/Properties")] = properties
    else:
        properties = properties.get_object()
    property_names: dict[str, NameObject] = {}
    for series_number, series in enumerate(SERIES, 1):
        name = NameObject(f"/Series{series_number}")
        properties[name] = ocg_refs[series.key]
        property_names[series.key] = name

    root_structure = DictionaryObject({NameObject("/Type"): NameObject("/StructTreeRoot")})
    root_structure_ref = writer._add_object(root_structure)
    series_structure_refs = []
    point_structure_refs = []
    mcid = 0
    for series in SERIES:
        series_structure = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/StructElem"),
                NameObject("/S"): NameObject("/Sect"),
                NameObject("/P"): root_structure_ref,
                NameObject("/T"): pdf_string(series.label),
            }
        )
        series_structure_ref = writer._add_object(series_structure)
        series_structure_refs.append(series_structure_ref)
        children = ArrayObject()
        for point in points[series.key]:
            point_structure = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/StructElem"),
                    NameObject("/S"): NameObject("/Figure"),
                    NameObject("/P"): series_structure_ref,
                    NameObject("/Pg"): page.indirect_reference,
                    NameObject("/K"): NumberObject(mcid),
                    NameObject("/T"): pdf_string(point.identifier),
                    NameObject("/Alt"): pdf_string(point.description),
                }
            )
            point_ref = writer._add_object(point_structure)
            children.append(point_ref)
            point_structure_refs.append(point_ref)
            mcid += 1
        series_structure[NameObject("/K")] = children

    parent_tree = DictionaryObject(
        {
            NameObject("/Nums"): ArrayObject(
                [NumberObject(0), ArrayObject(point_structure_refs)]
            )
        }
    )
    root_structure[NameObject("/K")] = ArrayObject(series_structure_refs)
    root_structure[NameObject("/ParentTree")] = writer._add_object(parent_tree)
    root_structure[NameObject("/ParentTreeNextKey")] = NumberObject(1)

    fill_to_point = dict(zip(fill_indices, all_points, strict=True))
    point_mcids = {point: index for index, point in enumerate(all_points)}
    rewritten = []
    for index, operation in enumerate(content.operations):
        point = fill_to_point.get(index)
        if point is not None:
            properties_dict = DictionaryObject(
                {
                    NameObject("/MCID"): NumberObject(point_mcids[point]),
                    NameObject("/ActualText"): pdf_string(point.description),
                    NameObject("/ID"): pdf_string(point.identifier),
                    NameObject("/Series"): pdf_string(point.series.key),
                    NameObject("/Year"): pdf_string(point.year),
                    NameObject("/Value"): pdf_string(point.value),
                    NameObject("/SourceLine"): NumberObject(point.source_line),
                }
            )
            rewritten.append(([NameObject("/OC"), property_names[point.series.key]], b"BDC"))
            rewritten.append(([NameObject("/Figure"), properties_dict], b"BDC"))
            rewritten.append(([], b"q"))
            rewritten.append(([point_opacity_name], b"gs"))
            rewritten.append(operation)
            rewritten.append(([], b"Q"))
            rewritten.append(([], b"EMC"))
            rewritten.append(([], b"EMC"))
        else:
            rewritten.append(operation)
    content.operations = rewritten
    page[NameObject("/Contents")] = writer._add_object(content)
    page[NameObject("/StructParents")] = NumberObject(0)

    ocg_array = ArrayObject([ocg_refs[series.key] for series in SERIES])
    writer.root_object[NameObject("/OCProperties")] = DictionaryObject(
        {
            NameObject("/OCGs"): ocg_array,
            NameObject("/D"): DictionaryObject(
                {
                    NameObject("/Name"): pdf_string("Data series"),
                    NameObject("/Order"): ocg_array,
                    NameObject("/ON"): ocg_array,
                }
            ),
        }
    )
    writer.root_object[NameObject("/StructTreeRoot")] = root_structure_ref
    writer.root_object[NameObject("/MarkInfo")] = DictionaryObject(
        {
            NameObject("/Marked"): BooleanObject(True),
            NameObject("/Suspects"): BooleanObject(False),
        }
    )
    writer.root_object[NameObject("/PageMode")] = NameObject("/UseOC")
    writer.root_object[NameObject("/Lang")] = pdf_string("en-GB")
    with output_path.open("wb") as stream:
        writer.write(stream)


def structure_eps(raw_path: Path, output_path: Path, points: dict[str, list[Point]]) -> None:
    lines = raw_path.read_text(encoding="latin-1").splitlines()
    patterns = {
        series.key: re.compile(rf"^\s*[-+0-9.]+\s+[-+0-9.]+\s+{series.eps_marker}\s*$")
        for series in SERIES
    }
    matches: dict[str, list[int]] = {
        series.key: [index for index, line in enumerate(lines) if patterns[series.key].match(line)]
        for series in SERIES
    }
    for series in SERIES:
        expected = len(points[series.key])
        found = len(matches[series.key])
        if found != expected:
            raise RuntimeError(f"{series.key}: expected {expected} EPS markers, found {found}")

    point_at_line: dict[int, Point] = {}
    for series in SERIES:
        point_at_line.update(zip(matches[series.key], points[series.key], strict=True))

    output: list[str] = []
    for line_number, line in enumerate(lines):
        point = point_at_line.get(line_number)
        if point is None:
            output.append(line)
            continue
        if point.index == 1:
            output.extend(
                (
                    f"%%BeginObject: series-{point.series.key}",
                    f"%%Series: {point.series.key}",
                )
            )
        output.append(f"%%BeginObject: {point.identifier}")
        metadata = (
            f"series={point.series.key} index={point.index} year={point.year} "
            f"value={point.value} source-line={point.source_line}"
        )
        output.append(f"%%PointData: {metadata}")
        if point.comment:
            output.append(f"%%PointComment: {point.comment}")
        output.extend(("gsave", line, "grestore", "%%EndObject"))
        if point.index == len(points[point.series.key]):
            output.append("%%EndObject")
    output_path.write_text("\n".join(output) + "\n", encoding="latin-1")


def count_pdf_images(reader: PdfReader) -> int:
    seen: set[tuple[int | None, int | None]] = set()
    images = 0

    def inspect(resources: object) -> None:
        nonlocal images
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        xobjects = resources.get("/XObject", {}) if resources else {}
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        for reference in xobjects.values():
            key = (getattr(reference, "idnum", None), getattr(reference, "generation", None))
            if key in seen:
                continue
            seen.add(key)
            obj = reference.get_object()
            subtype = str(obj.get("/Subtype"))
            if subtype == "/Image":
                images += 1
            elif subtype == "/Form":
                inspect(obj.get("/Resources", {}))

    for page in reader.pages:
        inspect(page.get("/Resources", {}))
    return images


def verify_pdf_fonts_embedded(reader: PdfReader) -> None:
    """Require every PDF font resource to be self-contained.

    Cairo emits ordinary subset TrueType fonts for text and Type 3 fonts for
    the few enhanced-text glyphs. Type 3 fonts carry their glyph paths in
    /CharProcs, so they are self-contained without a /FontFile stream.
    """

    seen_resources: set[tuple[int | None, int | None] | int] = set()
    seen_fonts: set[tuple[int | None, int | None] | int] = set()
    font_count = 0

    def object_key(reference: object, resolved: object) -> tuple[int | None, int | None] | int:
        idnum = getattr(reference, "idnum", None)
        generation = getattr(reference, "generation", None)
        return (idnum, generation) if idnum is not None else id(resolved)

    def inspect_font(reference: object, context: str) -> None:
        nonlocal font_count
        font = reference.get_object() if hasattr(reference, "get_object") else reference
        key = object_key(reference, font)
        if key in seen_fonts:
            return
        seen_fonts.add(key)
        font_count += 1

        subtype = str(font.get("/Subtype"))
        if subtype == "/Type0":
            descendants = font.get("/DescendantFonts", [])
            descendants = (
                descendants.get_object() if hasattr(descendants, "get_object") else descendants
            )
            if not descendants:
                raise RuntimeError(f"PDF font {context} has no descendant font")
            for index, descendant in enumerate(descendants):
                inspect_font(descendant, f"{context}/DescendantFonts[{index}]")
            return

        if subtype == "/Type3":
            char_procs = font.get("/CharProcs")
            char_procs = char_procs.get_object() if hasattr(char_procs, "get_object") else char_procs
            if not char_procs:
                raise RuntimeError(f"PDF Type 3 font {context} has no embedded glyph paths")
            return

        descriptor = font.get("/FontDescriptor")
        descriptor = descriptor.get_object() if hasattr(descriptor, "get_object") else descriptor
        if not descriptor or not any(
            descriptor.get(font_file) is not None
            for font_file in ("/FontFile", "/FontFile2", "/FontFile3")
        ):
            base_font = font.get("/BaseFont", "unnamed")
            raise RuntimeError(f"PDF font {context} ({base_font}) is not embedded")

    def inspect_resources(reference: object, context: str) -> None:
        resources = reference.get_object() if hasattr(reference, "get_object") else reference
        key = object_key(reference, resources)
        if key in seen_resources:
            return
        seen_resources.add(key)

        fonts = resources.get("/Font", {}) if resources else {}
        fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
        for name, font in fonts.items():
            inspect_font(font, f"{context}{name}")

        xobjects = resources.get("/XObject", {}) if resources else {}
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        for name, xobject_reference in xobjects.items():
            xobject = xobject_reference.get_object()
            if str(xobject.get("/Subtype")) == "/Form":
                inspect_resources(xobject.get("/Resources", {}), f"{context}{name}/")

    for page_number, page in enumerate(reader.pages, 1):
        inspect_resources(page.get("/Resources", {}), f"page-{page_number}/")
    if font_count == 0:
        raise RuntimeError("PDF contains no font resources")


def verify_svg(path: Path, points: dict[str, list[Point]]) -> None:
    root = ET.parse(path).getroot()
    if any(element.tag == f"{{{SVG_NS}}}image" for element in root.iter()):
        raise RuntimeError("SVG contains a raster image")
    identifiers = []
    for series in SERIES:
        group = root.find(f".//*[@id='series-{series.key}']")
        if group is None:
            raise RuntimeError(f"SVG is missing series-{series.key}")
        point_groups = [
            element for element in group.iter(f"{{{SVG_NS}}}g") if element.get("class") == "data-point"
        ]
        if len(point_groups) != len(points[series.key]):
            raise RuntimeError(f"SVG point count mismatch for {series.key}")
        for element, point in zip(point_groups, points[series.key], strict=True):
            expected_attributes = point_attributes(point)
            if any(element.get(name) != value for name, value in expected_attributes.items()):
                raise RuntimeError(f"SVG metadata mismatch for {point.identifier}")
            title = element.find(f"{{{SVG_NS}}}title")
            if title is None or title.text != point.description:
                raise RuntimeError(f"SVG title mismatch for {point.identifier}")
            if len(element.findall(f"{{{SVG_NS}}}use")) != 1:
                raise RuntimeError(f"SVG marker path mismatch for {point.identifier}")
            identifiers.append(element.get("id", ""))
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("SVG point IDs are not unique")
    groups = list(root.iter(f"{{{SVG_NS}}}g"))
    group_ids = [group.get("id", "") for group in groups]
    if any(not identifier for identifier in group_ids):
        raise RuntimeError("SVG contains anonymous groups")
    if len(group_ids) != len(set(group_ids)):
        raise RuntimeError("SVG group IDs are not unique")
    chart = root.find(f"{{{SVG_NS}}}g[@id='chart']")
    if chart is None:
        raise RuntimeError("SVG is missing its named chart group")
    expected_components = {
        "chart-background",
        "chart-grid",
        "data-series",
        "chart-axes",
        "series-labels",
        "chart-annotations",
        "chart-title",
    }
    component_ids = {
        child.get("id") for child in chart.findall(f"{{{SVG_NS}}}g")
    }
    if component_ids != expected_components:
        raise RuntimeError(f"SVG chart component groups are incorrect: {component_ids}")
    semantic_counts = {
        "horizontal grid lines": len(root.findall(f".//*[@id='grid-horizontal']/*[@class='gridline']")),
        "vertical grid lines": len(root.findall(f".//*[@id='grid-vertical']/*[@class='gridline']")),
        "x-axis ticks": len(root.findall(f".//*[@id='x-axis-ticks']/*[@class='axis-tick']")),
        "y-axis ticks": len(root.findall(f".//*[@id='y-axis-ticks']/*[@class='axis-tick']")),
        "series labels": len(root.findall(f".//*[@id='series-labels']/*[@class='series-label']")),
        "annotations": len(root.findall(f".//*[@id='chart-annotations']/*[@class='chart-annotation']")),
    }
    expected_semantic_counts = {
        "horizontal grid lines": 8,
        "vertical grid lines": 6,
        "x-axis ticks": 6,
        "y-axis ticks": 8,
        "series labels": 5,
        "annotations": 2,
    }
    if semantic_counts != expected_semantic_counts:
        raise RuntimeError(f"SVG semantic group counts are incorrect: {semantic_counts}")
    font_elements = [element for element in root.iter() if "font-family" in element.attrib]
    if not font_elements or any(
        element.get("font-family") != SVG_FONT_STACK for element in font_elements
    ):
        raise RuntimeError("SVG does not consistently use the portable font fallback stack")
    serialized = path.read_text(encoding="utf-8").lower()
    if any(token in serialized for token in ("@font-face", "data:font", "<font", "font-face")):
        raise RuntimeError("SVG unexpectedly contains embedded font data")


def verify_pdf(path: Path, points: dict[str, list[Point]]) -> None:
    reader = PdfReader(path)
    if count_pdf_images(reader):
        raise RuntimeError("PDF contains a raster image")
    verify_pdf_fonts_embedded(reader)
    oc_properties = reader.trailer["/Root"].get("/OCProperties")
    if oc_properties is None or len(oc_properties.get_object()["/OCGs"]) != len(SERIES):
        raise RuntimeError("PDF does not contain five series layers")
    structure = reader.trailer["/Root"].get("/StructTreeRoot")
    if structure is None or len(structure.get_object()["/K"]) != len(SERIES):
        raise RuntimeError("PDF structure tree does not contain five series")
    structure_series = structure.get_object()["/K"]
    expected_identifiers = [
        point.identifier for series in SERIES for point in points[series.key]
    ]
    structure_identifiers: list[str] = []
    for series_element, series in zip(structure_series, SERIES, strict=True):
        series_element = series_element.get_object()
        children = series_element.get("/K", [])
        if str(series_element.get("/S")) != "/Sect" or len(children) != len(points[series.key]):
            raise RuntimeError(f"PDF structure tree mismatch for {series.key}")
        for child, point in zip(children, points[series.key], strict=True):
            child = child.get_object()
            if (
                str(child.get("/S")) != "/Figure"
                or str(child.get("/T")) != point.identifier
                or str(child.get("/Alt")) != point.description
            ):
                raise RuntimeError(f"PDF structure metadata mismatch for {point.identifier}")
            structure_identifiers.append(str(child.get("/T")))
    if structure_identifiers != expected_identifiers:
        raise RuntimeError("PDF structure point order does not match the source data")
    operations = ContentStream(reader.pages[0].get_contents(), reader).operations
    figures = [
        operands
        for operands, operator in operations
        if operator == b"BDC" and operands and str(operands[0]) == "/Figure"
    ]
    expected = sum(len(series_points) for series_points in points.values())
    if len(figures) != expected:
        raise RuntimeError(f"expected {expected} PDF point groups, found {len(figures)}")
    opacity_uses = [
        operands
        for operands, operator in operations
        if operator == b"gs" and operands and str(operands[0]) == "/DataPointOpacity"
    ]
    if len(opacity_uses) != expected:
        raise RuntimeError(
            f"expected {expected} PDF point-opacity applications, found {len(opacity_uses)}"
        )
    ext_gstates = reader.pages[0]["/Resources"].get("/ExtGState", {}).get_object()
    point_opacity = ext_gstates.get("/DataPointOpacity")
    point_opacity = point_opacity.get_object() if point_opacity is not None else None
    if (
        point_opacity is None
        or abs(float(point_opacity.get("/ca", 1)) - POINT_OPACITY) > 0.001
        or abs(float(point_opacity.get("/CA", 1)) - POINT_OPACITY) > 0.001
    ):
        raise RuntimeError("PDF data-point opacity is missing or incorrect")
    marked_identifiers = [str(properties.get("/ID")) for _, properties in figures]
    mcids = [int(properties.get("/MCID")) for _, properties in figures]
    if marked_identifiers != expected_identifiers or mcids != list(range(expected)):
        raise RuntimeError("PDF marked point metadata does not match the source data")


def verify_eps(path: Path, points: dict[str, list[Point]]) -> None:
    text = path.read_text(encoding="latin-1")
    executable = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("%"))
    if re.search(r"\b(?:image|colorimage|imagemask)\b", executable, re.IGNORECASE):
        raise RuntimeError("EPS contains a raster image operator")
    if text.count("%%BeginObject:") != text.count("%%EndObject"):
        raise RuntimeError("EPS object groups are unbalanced")
    expected = sum(len(series_points) for series_points in points.values())
    if text.count("%%PointData:") != expected:
        raise RuntimeError("EPS point group count mismatch")
    if "%%DocumentFonts: Helvetica" not in text:
        raise RuntimeError("EPS does not declare the standard Helvetica font")
    if re.search(r"^%%BeginFont\b|^%%IncludeFont\b", text, re.MULTILINE):
        raise RuntimeError("EPS unexpectedly contains an embedded or included font")
    for series in SERIES:
        if text.count(f"%%BeginObject: series-{series.key}") != 1:
            raise RuntimeError(f"EPS is missing series-{series.key}")
        for point in points[series.key]:
            if text.count(f"%%BeginObject: {point.identifier}\n") != 1:
                raise RuntimeError(f"EPS is missing {point.identifier}")
            metadata = (
                f"%%PointData: series={series.key} index={point.index} year={point.year} "
                f"value={point.value} source-line={point.source_line}"
            )
            if text.count(metadata) != 1:
                raise RuntimeError(f"EPS metadata mismatch for {point.identifier}")


def verify_png(path: Path, edition: Edition) -> None:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("PNG has an invalid signature")
    dimensions = struct.unpack(">II", header[16:24])
    expected = (edition.width, edition.height)
    if dimensions != expected:
        raise RuntimeError(f"PNG dimensions are {dimensions}, expected {expected}")


def install_output(raw_path: Path, output_path: Path) -> None:
    os.replace(raw_path, output_path)


def generate(formats: Iterable[str], editions: Iterable[Edition]) -> None:
    if shutil.which("gnuplot") is None:
        raise SystemExit("gnuplot is required; install it with 'brew install gnuplot' or your package manager")

    with tempfile.TemporaryDirectory(prefix="processor-trend-") as temporary:
        temp_dir = Path(temporary)
        staged: dict[tuple[str, str], Path] = {}
        for edition in editions:
            points = parse_points(edition)
            actual_counts = tuple(len(points[series.key]) for series in SERIES)
            if actual_counts != edition.expected_counts:
                raise RuntimeError(
                    f"{edition.years}-year source point counts are {actual_counts}, "
                    f"expected {edition.expected_counts}"
                )
            for fmt in formats:
                raw_path = temp_dir / f"{edition.years}-raw.{fmt}"
                structured_path = temp_dir / f"{edition.years}-structured.{fmt}"
                render_raw(fmt, raw_path, edition)
                if fmt == "svg":
                    structure_svg(raw_path, structured_path, points, edition)
                    verify_svg(structured_path, points)
                elif fmt == "pdf":
                    structure_pdf(raw_path, structured_path, points)
                    verify_pdf(structured_path, points)
                elif fmt == "eps":
                    structure_eps(raw_path, structured_path, points)
                    verify_eps(structured_path, points)
                else:
                    structured_path = raw_path
                    verify_png(structured_path, edition)
                staged[(edition.key, fmt)] = structured_path

        for edition in editions:
            for fmt in formats:
                output_path = edition.directory / f"{edition.basename}.{fmt}"
                install_output(staged[(edition.key, fmt)], output_path)
                print(f"generated {output_path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=FORMATS,
        default=list(FORMATS),
        help="formats to generate (default: png svg pdf eps)",
    )
    parser.add_argument(
        "--editions",
        nargs="+",
        choices=tuple(EDITIONS),
        default=list(EDITIONS),
        help="year editions to generate (default: 40 42 48 50)",
    )
    args = parser.parse_args()
    requested = list(dict.fromkeys(args.formats))
    selected_editions = [EDITIONS[key] for key in dict.fromkeys(args.editions)]
    generate(requested, selected_editions)


if __name__ == "__main__":
    main()
