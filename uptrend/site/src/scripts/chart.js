const data = JSON.parse(document.getElementById("chart-data").textContent);
const svg = document.querySelector("#interactive-chart svg");
const series = svg.querySelector("#chart-series");
const panel = document.getElementById("device-panel");
const picker = document.getElementById("device-picker");
const bubble = document.getElementById("hover-bubble");
const byDevice = new Map(
  data.devices.map((device) => [device.device_id, device]),
);
const pointGroups = new Map(
  [...svg.querySelectorAll(".data-point")].map((group) => [group.id, group]),
);
const rawPoints = data.devices.flatMap((device) =>
  device.plotted_points.map((point) => ({
    ...point,
    device_id: device.device_id,
  })),
);
if (
  pointGroups.size !== rawPoints.length ||
  rawPoints.some((point) => !pointGroups.has(point.observation_id))
) {
  throw new Error(
    "The SVG markers and device data are out of sync. Regenerate the chart before building the site.",
  );
}
for (const point of rawPoints) {
  const device = byDevice.get(point.device_id);
  const gpuComponent =
    device.device_class === "gpu" ||
    (point.metric === "fp32_dense_peak_gflops" &&
      ["soc", "manycore"].includes(device.device_class));
  pointGroups
    .get(point.observation_id)
    .classList.add(gpuComponent ? "marker-gpu" : "marker-cpu");
}

// Every native SVG title is removed at build time; no browser-default tooltip can cover ours.
if (svg.querySelector("title"))
  throw new Error("The chart still contains native SVG tooltips.");

const points = rawPoints.map((point) => {
  const box = pointGroups.get(point.observation_id).getBBox();
  return { ...point, x: box.x + box.width / 2, y: box.y + box.height / 2 };
});
const pointsById = new Map(
  points.map((point) => [point.observation_id, point]),
);
const plot = svg.querySelector("#plot-frame").getBBox();
const groupsByDevice = new Map();
for (const group of pointGroups.values()) {
  const deviceId = group.dataset.deviceId;
  if (!groupsByDevice.has(deviceId)) groupsByDevice.set(deviceId, []);
  groupsByDevice.get(deviceId).push(group);
}
for (const device of data.devices) {
  const expectedIds = new Set(
    device.plotted_points.map((point) => point.observation_id),
  );
  const actual = groupsByDevice.get(device.device_id) || [];
  if (
    actual.length !== expectedIds.size ||
    actual.some((group) => !expectedIds.has(group.id))
  ) {
    throw new Error(`SVG markers do not match ${device.device_id}.`);
  }
}
function setDeviceActive(deviceId, active) {
  for (const group of groupsByDevice.get(deviceId) || [])
    group.classList.toggle("is-active", active);
}
const ns = "http://www.w3.org/2000/svg";
const guide = document.createElementNS(ns, "line");
guide.id = "interaction-guide";
guide.setAttribute("y1", String(plot.y));
guide.setAttribute("y2", String(plot.y + plot.height));
guide.setAttribute("stroke", "#65829f");
guide.setAttribute("stroke-width", "1.5");
guide.setAttribute("stroke-dasharray", "6 6");
guide.setAttribute("visibility", "hidden");
svg.querySelector("#chart").insertBefore(guide, series);

const metricOrder = [
  ["transistors_million", "Transistors"],
  ["frequency_ghz", "Clock frequency"],
  ["power_w", "Power"],
  ["cpu_physical_cores", "Physical CPU cores"],
  ["cpu_single_thread_normalized", "CPU single-core index"],
  ["cpu_multicore_index", "CPU multi-core index"],
  ["fp32_dense_peak_gflops", "Dense FP32 peak"],
];
const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });
const number = (value) =>
  value !== 0 && Math.abs(value) < 0.0001
    ? value.toExponential(2)
    : fmt.format(value);
const node = (tag, className = "", text = "") => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
};
const applicable = (device, metric) =>
  !(device.device_class === "gpu" && metric.startsWith("cpu_")) &&
  !(device.device_class === "cpu" && metric === "fp32_dense_peak_gflops");
function metricText(metric, detail) {
  if (detail?.value == null)
    return detail?.validation_status?.startsWith("not_applicable")
      ? "Not applicable"
      : "Not documented";
  const suffix =
    {
      transistors_million: " million",
      frequency_ghz: " GHz",
      power_w: " W",
      cpu_physical_cores: " cores",
      fp32_dense_peak_gflops: " GFLOP/s",
    }[metric] || "";
  return number(detail.value) + suffix;
}
function metricState(detail) {
  if (detail.value == null)
    return detail.notes || "No supported value is available for this device.";
  const kind =
    detail.value_kind === "direct"
      ? "Reported"
      : detail.value_kind === "measured"
        ? "Measured"
        : detail.value_kind === "calculated"
          ? "Calculated estimate"
          : detail.value_kind === "normalized"
            ? "Estimated index"
            : detail.value_kind;
  return `${kind} · ${detail.source_tier || "source unspecified"} · ${detail.confidence} confidence${detail.plotted ? "" : " · not plotted"}`;
}
function evidence(detail) {
  const wrapper = node("details");
  wrapper.append(node("summary", "", "Source and method"));
  const body = node("div", "evidence");
  if (detail.scope) body.append(node("div", "", `Scope: ${detail.scope}`));
  if (detail.raw_benchmark_suite)
    body.append(
      node(
        "div",
        "",
        `${detail.raw_benchmark_suite}: ${detail.raw_benchmark_value ?? "unavailable"}`,
      ),
    );
  if (detail.normalization) body.append(node("div", "", detail.normalization));
  if (detail.clock_basis)
    body.append(node("div", "", `Clock basis: ${detail.clock_basis}`));
  if (detail.calculation_expression)
    body.append(
      node("div", "", `Calculation: ${detail.calculation_expression}`),
    );
  if (detail.calculation_operands)
    body.append(
      node("div", "", `Inputs: ${JSON.stringify(detail.calculation_operands)}`),
    );
  if (detail.notes) body.append(node("div", "", detail.notes));
  for (const reference of detail.reference_urls || []) {
    try {
      const url = new URL(reference);
      if (!["https:", "http:"].includes(url.protocol)) continue;
      const link = node("a", "", `${url.hostname}${url.pathname}`);
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      body.append(link);
    } catch {
      /* Ignore malformed legacy references. */
    }
  }
  wrapper.append(body);
  return wrapper;
}

let activeDevice = null;
let activePoint = null;
let nearby = [];
function renderPanel(device, candidates) {
  panel.replaceChildren();
  const head = node("div", "device-head");
  const heading = node("div");
  heading.append(node("h2", "", device.name));
  heading.append(
    node(
      "p",
      "device-meta",
      `${device.vendor} · ${device.device_class.toUpperCase()} · Released ${device.release_date}`,
    ),
  );
  head.append(heading);
  panel.append(head);
  const grid = node("div", "metric-grid");
  for (const [metric, label] of metricOrder) {
    if (!applicable(device, metric)) continue;
    const detail = device.metrics[metric];
    const card = node("div", "metric-card");
    card.append(node("div", "metric-name", label));
    card.append(node("strong", "metric-value", metricText(metric, detail)));
    card.append(node("div", "metric-state", metricState(detail)));
    if (detail.value != null || detail.notes) card.append(evidence(detail));
    grid.append(card);
  }
  panel.append(grid);
  if (candidates.length > 1) {
    const section = node("section", "nearby");
    section.append(
      node("h3", "", `Overlapping or nearby devices (${candidates.length})`),
    );
    const list = node("div", "nearby-list");
    for (const candidate of candidates) {
      const other = byDevice.get(candidate.device_id);
      const button = node(
        "button",
        "",
        `${other.name} · ${other.release_date}`,
      );
      button.type = "button";
      button.setAttribute(
        "aria-current",
        String(candidate.device_id === device.device_id),
      );
      button.addEventListener("click", () => {
        renderPanel(other, candidates);
        bubble.hidden = true;
      });
      list.append(button);
    }
    section.append(list);
    panel.append(section);
  }
}
function activate(deviceId, pointId, candidates = []) {
  const device = byDevice.get(deviceId);
  if (!device) return;
  if (activeDevice) setDeviceActive(activeDevice, false);
  activeDevice = deviceId;
  activePoint =
    pointsById.get(pointId) ||
    points.find((point) => point.device_id === deviceId);
  nearby = candidates;
  setDeviceActive(deviceId, true);
  series.classList.add("is-focused");
  if (activePoint) {
    guide.setAttribute("x1", String(activePoint.x));
    guide.setAttribute("x2", String(activePoint.x));
    guide.setAttribute("visibility", "visible");
  }
}
function clearFocus() {
  if (activeDevice) setDeviceActive(activeDevice, false);
  activeDevice = null;
  activePoint = null;
  nearby = [];
  series.classList.remove("is-focused");
  guide.setAttribute("visibility", "hidden");
  bubble.hidden = true;
}
function renderEmptyPanel() {
  panel.replaceChildren();
  const empty = node("div", "panel-empty");
  empty.append(
    node("h2", "", "Device details"),
    node(
      "p",
      "",
      "Hover for a quick summary. Click a chart marker to see its measurements and sources here.",
    ),
  );
  panel.append(empty);
}
function nearest(event) {
  const matrix = svg.getScreenCTM();
  if (!matrix) return null;
  const cursor = svg.createSVGPoint();
  cursor.x = event.clientX;
  cursor.y = event.clientY;
  const local = cursor.matrixTransform(matrix.inverse());
  if (
    local.x < plot.x - 10 ||
    local.x > plot.x + plot.width + 10 ||
    local.y < plot.y - 10 ||
    local.y > plot.y + plot.height + 10
  )
    return null;
  const threshold = 18 / Math.hypot(matrix.a, matrix.b);
  let best = null;
  let distance = threshold;
  for (const point of points) {
    const candidateDistance = Math.hypot(point.x - local.x, point.y - local.y);
    if (candidateDistance < distance) {
      best = point;
      distance = candidateDistance;
    }
  }
  if (!best) return null;
  const candidates = [];
  const seen = new Set();
  for (const point of points
    .slice()
    .sort(
      (a, b) =>
        Math.hypot(a.x - local.x, a.y - local.y) -
        Math.hypot(b.x - local.x, b.y - local.y),
    )) {
    if (
      Math.hypot(point.x - local.x, point.y - local.y) >
      Math.min(threshold, distance + 7 / Math.hypot(matrix.a, matrix.b))
    )
      break;
    if (seen.has(point.device_id)) continue;
    seen.add(point.device_id);
    candidates.push({
      device_id: point.device_id,
      observation_id: point.observation_id,
    });
    if (candidates.length === 12) break;
  }
  return { point: best, candidates };
}
function showBubble(event, point) {
  const device = byDevice.get(point.device_id);
  bubble.replaceChildren();
  const heading = node("div", "bubble-heading");
  heading.append(
    node("div", "bubble-title", device.name),
    node("div", "bubble-year", device.release_date.slice(0, 4)),
  );
  bubble.append(heading);
  const metrics = node("dl", "bubble-metrics");
  for (const [metric, label] of metricOrder) {
    if (!applicable(device, metric)) continue;
    const row = node("div", "bubble-row");
    row.append(
      node("dt", "", label),
      node("dd", "", metricText(metric, device.metrics[metric])),
    );
    metrics.append(row);
  }
  bubble.append(metrics);
  bubble.hidden = false;
  const bounds = bubble.getBoundingClientRect();
  bubble.style.left = `${Math.max(8, Math.min(event.clientX + 16, innerWidth - bounds.width - 8))}px`;
  bubble.style.top = `${Math.max(8, Math.min(event.clientY + 16, innerHeight - bounds.height - 8))}px`;
}

const names = data.devices
  .filter((device) => device.plotted_points.length)
  .sort(
    (a, b) =>
      a.name.localeCompare(b.name) ||
      a.release_date.localeCompare(b.release_date),
  );
for (const device of names) {
  const option = node("option", "", `${device.name} (${device.release_date})`);
  option.value = device.device_id;
  picker.append(option);
}
picker.addEventListener("change", () => {
  const device = byDevice.get(picker.value);
  if (!device) return renderEmptyPanel();
  renderPanel(device, []);
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});
svg.addEventListener("pointermove", (event) => {
  const hit = nearest(event);
  if (!hit) {
    clearFocus();
    return;
  }
  const { point, candidates } = hit;
  const key = (list) =>
    list.map((item) => `${item.device_id}:${item.observation_id}`).join("|");
  if (
    activeDevice !== point.device_id ||
    activePoint?.observation_id !== point.observation_id ||
    key(nearby) !== key(candidates)
  ) {
    activate(point.device_id, point.observation_id, candidates);
  }
  showBubble(event, point);
});
svg.addEventListener("pointerleave", clearFocus);
svg.addEventListener("click", (event) => {
  const hit = nearest(event);
  if (!hit) return;
  renderPanel(byDevice.get(hit.point.device_id), hit.candidates);
  bubble.hidden = true;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
});

const themeButton = document.getElementById("theme-toggle");
function currentTheme() {
  return (
    document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
  );
}
function syncThemeButton() {
  const dark = currentTheme() === "dark";
  themeButton.setAttribute(
    "aria-label",
    `Switch to ${dark ? "light" : "dark"} mode`,
  );
  themeButton.setAttribute("aria-pressed", String(dark));
}
themeButton.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("compute-trends-theme", next);
  } catch {
    /* Storage can be disabled. */
  }
  syncThemeButton();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener(
  "change",
  syncThemeButton,
);
syncThemeButton();
