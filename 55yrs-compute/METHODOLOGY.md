# CPU and GPU compute-trends methodology

This edition covers shipping products through 2026-09-22. It retains the
historical processor records from the 50-year dataset and adds distinct Apple
M1-M6 SoCs, server/HPC CPUs, and data-center/HPC GPUs with metric-level references.
Retail clock-bin duplicates, systems, cloud instance sizes, roadmap products,
and AI-only accelerators are excluded. Historically important gaming GPUs are
included because GeForce and Radeon launches define much of the early GPU
timeline; unsupported fixed-function graphics rates are never converted to
FP32.

Each charted value has its own unit, scope, evidence type, confidence, and
reference in `55-years-compute-observations.csv`. Blank values are intentional:
the generator does not fill unsupported specifications.

`55-years-compute-chart-data.json` groups every observation by `device_id`.
All markers for a device use that device's release-date x-coordinate, so an
interactive chart can highlight its measurements together. Supported chart
observations are plotted without selecting an annual winner. Some
devices lack documented values; these remain `null` in the device profile and
are not inferred from neighboring chips. CPU and GPU metric applicability
differs. Physical CPU core counts are separate, device-linked markers, not a
connected frontier line.

The chart follows the original microprocessor-trends composition: all series
share one logarithmic field so their changes and plateaus can be compared over
time. Each series uses the unit and multiplier stated beside it: transistors
are shown as counts (the CSV stores millions), multi-core index x 10^4,
dense FP32 in GFLOP/s x 100, single-core index x 10^3, frequency in MHz,
power in W, and physical cores as the actual core count. Thus one core plots
at 10^0, one watt at 10^0, and any other ordinate can be recovered by dividing
its plotted position by the stated multiplier. These factors do not alter the
underlying values or trends. The shared y-axis has no common physical unit:
compare trends within a series, not absolute heights between series.
Exact metric values and units remain in the observation CSV and vector point
metadata. The black diamonds show a separate **estimated one-chip integer
throughput index** (2003 IBM POWER4+ = 100).
It must not be read directly against the single-thread series. The three
original one-chip SPEC rate scores remain in distinct CSV columns, unaltered.
Pre-1988 values identified by the historical source as performance proxies stay
in the observation ledger but are not plotted as benchmark observations.

Apple M-series single-core values use one consistent raw benchmark: Geekbench
7 single-core. The benchmark maker's [Mac chart](https://browser.geekbench.com/mac-benchmarks)
reports an average for each listed Mac from at least five unique submissions.
The newly shipping M5 Ultra and M6 instead use identified individual results,
so their evidence is weaker. The raw scores, tested Mac configurations, URLs,
and 2026-09-23 snapshot are in `apple-single-core-geekbench7.json` and the
observation CSV. To place these measurements on the historical graph, the
generator calculates `59.9 × (chip Geekbench 7 score / 2190)`, rounded to one
decimal. Here 59.9 is the legacy M1 SPECint-continuity estimate, and 2190 is
the Mac mini M1's Geekbench 7 score. This is a cross-benchmark **illustrative
index**, not a measured or official SPEC score; its relative M-series trend is
more defensible than interpreting its absolute height against other CPUs.
Older chips were benchmarked under later software and are plotted at their
original release dates, not at the 2026 benchmark date.
The CSV retains each raw suite and score, so the estimate can be replaced if
same-suite SPEC measurements become available.

Within a calendar year the chart keeps the leading documented product for each
vendor and market class. Competing vendors remain visible, and a headline
client GPU may coexist with a datacenter accelerator. Weaker same-vendor,
same-class variants remain in the observation ledger with
`supporting_same_year_not_plotted` status.

Dense FP32 peak is a separate GPU-only overlaid series. It excludes tensor,
matrix-only, sparse, FP16/BF16, FP8, and integer throughput. Equal FP32 values
do not imply equal application performance across architectures. The plot has
23 discrete GPU observations and one GPU-component observation for MI300A.
The isolated Graviton2 and A64FX CPU FP32 values remain in the observation
ledger but are not plotted: two CPU points would imply a CPU trajectory that
the current evidence does not support. Early GPU years with unsupported FP32
values remain blank rather than being interpolated. FP32 markers are slightly
smaller to keep the other trend lines legible where they cross.

The CPU multi-core overlay starts with published integer-base rate results
from SPEC CPU2000, CPU2006, and CPU2017 systems with exactly one CPU package.
SPEC Rate runs multiple independent benchmark copies; it measures
aggregate work per unit time, not the speedup of one parallel job. Cache,
memory, frequency, software, and simultaneous execution all affect the result.
It is not a core count and does not imply identical application performance.
The suites are incompatible. For an **educational estimate only**, the generator
bridges CPU2000→CPU2006 and CPU2006→CPU2017 using the median log-ratio from
published results on matched one-chip processor models. It sets the 2003
POWER4+ CPU2000 score (15.2) to index 100 and applies the bridge factors to
later raw scores. The complete bridge pairs, exact scores, systems, and SPEC
URLs are in `cpu-throughput-bridges.json`; the generated observation ledger
records the raw suite/score, calculated index, factors, and citations for every
point. The CPU2006→CPU2017 bridge has only AMD EPYC 7601 paired reports, so
cross-architecture precision is limited. [SPEC explicitly advises against
direct suite comparisons](https://www.spec.org/cpu2017/Docs/overview.html);
this index is not an official conversion or a predictive benchmark. Historical
CPUs without a qualifying one-chip result remain blank; no
core-count-times-single-thread estimate is used. Each point is plotted at the
chip's release year even if the cited system was tested later. The multi-core
index is a scatter series, without connecting lines or interpolation.

Physical CPU cores are shown as a stepwise record frontier, beginning with
one core for the 1971 Intel 4004 and reaching two with IBM POWER4 in 2001.
The 2001 POWER4 step remains, but its marker and callout are omitted from the
figure. Other increases among cited general-purpose CPU-package records
receive a marker; the horizontal steps describe the highest count in this curated
dataset until the next cited record. This is an architectural milestone
series, not a performance measure or a claim about the first multi-core CPU
ever made. Heterogeneous Cell processing elements, the unshipped Rock design,
and Xeon Phi accelerator-class records do not define this frontier.
All other core counts remain in the observation CSV as supporting data.
Hardware-thread contexts, SMT, and Hyper-Threading are not counted or
plotted. Legacy rows that originally recorded logical threads are reconciled
to physical-core counts in `physical-core-overrides.json`.
Vendor-native GPU SM/CU/GPU-core counts stay in the observation ledger as
supporting specifications but are not plotted because those units are not
comparable across architectures or vendors. Power values retain their reported
scope, such as CPU TDP, GPU TBP/TDP, package power, or a documented estimate.

Recent Apple specifications in `apple-supplemental-metrics.json` use clearly
identified secondary reports where Apple has not published a count, clock, or
package-power figure. The M4 Pro transistor value is an approximate
cross-generation ratio calculation; M4 Max and M5 counts are secondary reports,
not Apple disclosures. M5 Pro, M5 Max, M5 Ultra, and M6 transistor totals remain
blank because a defensible exact or calculated value was not found. The
interactive site shows these gaps explicitly rather than inventing precise
values from core count or process node.

SVG and PDF use 72% point opacity. Filled markers identify discrete GPUs and
GPU-specific components (including MI300A FP32); CPU and other package-level
observations are outlined. Fill no longer encodes evidence quality. The exact
evidence type, confidence, and citation remain in the observation CSV and
vector point metadata. EPS keeps each point as an independent vector object
but cannot represent standard cross-editor alpha transparency or a persistent
layer tree.
