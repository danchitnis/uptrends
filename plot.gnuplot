if (!exists("OUTPUT_TERMINAL")) OUTPUT_TERMINAL = "postscript eps color enhanced"
if (!exists("OUTPUT_FILE")) OUTPUT_FILE = "processor-trend.eps"
if (!exists("PLOT_TITLE")) PLOT_TITLE = "Microprocessor Trend Data"
if (!exists("UPDATE_ATTRIBUTION")) UPDATE_ATTRIBUTION = "New plot and data collected by K. Rupp"
if (!exists("X_RANGE_MAX")) X_RANGE_MAX = 2022.5
if (!exists("Y_RANGE_MAX")) Y_RANGE_MAX = 7e7
if (!exists("LABEL_X")) LABEL_X = 2023
if (!exists("DIRECT_LAYOUT")) DIRECT_LAYOUT = 0
if (!exists("LEFT_MARGIN")) LEFT_MARGIN = 0.08
if (!exists("RIGHT_MARGIN")) RIGHT_MARGIN = 0.80
if (!exists("BOTTOM_MARGIN")) BOTTOM_MARGIN = 0.18
if (!exists("TOP_MARGIN")) TOP_MARGIN = 0.83
if (!exists("MARKER_SCALE")) MARKER_SCALE = 1.0
if (!exists("ATTRIBUTION_FONT")) ATTRIBUTION_FONT = ",8"
if (!exists("XTIC_Y_OFFSET")) XTIC_Y_OFFSET = 0.0
if (!exists("YTIC_X_OFFSET")) YTIC_X_OFFSET = 0.0
if (!exists("XLABEL_Y_OFFSET")) XLABEL_Y_OFFSET = 0.0
if (!exists("TITLE_Y_OFFSET")) TITLE_Y_OFFSET = 0.0
if (!exists("ATTRIBUTION_Y_OFFSET")) ATTRIBUTION_Y_OFFSET = 0.0
if (!exists("MARKER_SCALE_CORES")) MARKER_SCALE_CORES = MARKER_SCALE
if (!exists("MARKER_SCALE_FREQUENCY")) MARKER_SCALE_FREQUENCY = MARKER_SCALE
if (!exists("MARKER_SCALE_SPECINT")) MARKER_SCALE_SPECINT = MARKER_SCALE
if (!exists("MARKER_SCALE_TRANSISTORS")) MARKER_SCALE_TRANSISTORS = MARKER_SCALE
if (!exists("MARKER_SCALE_WATTS")) MARKER_SCALE_WATTS = MARKER_SCALE
if (!exists("POINT_COLOR_CORES")) POINT_COLOR_CORES = "#000000"
if (!exists("POINT_COLOR_FREQUENCY")) POINT_COLOR_FREQUENCY = "#008800"
if (!exists("POINT_COLOR_SPECINT")) POINT_COLOR_SPECINT = "#0000BB"
if (!exists("POINT_COLOR_TRANSISTORS")) POINT_COLOR_TRANSISTORS = "#CC6600"
if (!exists("POINT_COLOR_WATTS")) POINT_COLOR_WATTS = "#BB0000"

eval "set terminal " . OUTPUT_TERMINAL
set output OUTPUT_FILE

if (DIRECT_LAYOUT) {
  set lmargin at screen LEFT_MARGIN
  set rmargin at screen RIGHT_MARGIN
  set bmargin at screen BOTTOM_MARGIN
  set tmargin at screen TOP_MARGIN
  set size 1.0
} else {
  set rmargin at screen 0.64
  set size 0.8
  set size ratio 0.63
}

set style line 1 lt 1 ps MARKER_SCALE_CORES       pt 13 lc rgb POINT_COLOR_CORES
set style line 2 lt 1 ps MARKER_SCALE_FREQUENCY   pt 5  lc rgb POINT_COLOR_FREQUENCY
set style line 3 lt 1 ps MARKER_SCALE_SPECINT     pt 7  lc rgb POINT_COLOR_SPECINT
set style line 4 lt 1 ps MARKER_SCALE_TRANSISTORS pt 9  lc rgb POINT_COLOR_TRANSISTORS
set style line 5 lt 1 ps MARKER_SCALE_WATTS       pt 11 lc rgb POINT_COLOR_WATTS

# Keep all text opaque; only plotted markers use the translucent colors.
set style line 11 lt 1 lc rgb "#000000"
set style line 12 lt 1 lc rgb "#008800"
set style line 13 lt 1 lc rgb "#0000BB"
set style line 14 lt 1 lc rgb "#CC6600"
set style line 15 lt 1 lc rgb "#BB0000"

set border lw 1.5

if (DIRECT_LAYOUT) {
  set xlabel "Year" offset 0,XLABEL_Y_OFFSET
  set xtics offset 0,XTIC_Y_OFFSET
  set ytics offset YTIC_X_OFFSET,0
} else {
  set xlabel "Year"
}

set grid
unset key
set logscale y
set format y "10^{%T}"

set xrange [1970:X_RANGE_MAX]
set yrange [0.2:Y_RANGE_MAX]
unset mytics

set label "Number of"     at LABEL_X,2e1 tc ls 11
set label "Logical Cores" at LABEL_X,6.6e0 tc ls 11

set label "Frequency (MHz)"    at LABEL_X,3e3 tc ls 12

set label "Single-Thread"      at LABEL_X,1.5e5 tc ls 13
set label "Performance"        at LABEL_X,5e4 tc ls 13
set label "(SpecINT x 10^{3})" at LABEL_X,1.9e4 tc ls 13

set label "Transistors"   at LABEL_X,6e6 tc ls 14
set label "(thousands)"   at LABEL_X,2e6 tc ls 14

set label "Typical Power" at LABEL_X,3e2 tc ls 15
set label "(Watts)"       at LABEL_X,1e2 tc ls 15

set title PLOT_TITLE offset 0,TITLE_Y_OFFSET

set label "Original data up to the year 2010 collected and plotted by M. Horowitz, F. Labonte, O. Shacham, K. Olukotun, L. Hammond, and C. Batten" at 1970,6e-3 tc ls 11 font ATTRIBUTION_FONT offset 0,ATTRIBUTION_Y_OFFSET
set label UPDATE_ATTRIBUTION at 1970,3e-3 tc ls 11 font ATTRIBUTION_FONT offset 0,ATTRIBUTION_Y_OFFSET

plot \
 "cores.dat"        using 1:2 ls 1 with points, \
 "frequency.dat"    using 1:2 ls 2 with points, \
 "specint.dat"      using 1:2 ls 3 with points, \
 "transistors.dat"  using 1:2 ls 4 with points, \
 "watts.dat"        using 1:2 ls 5 with points
