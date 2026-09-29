# Siglent live differential viewer

`main.py` repeatedly acquires coherent four-channel frames from the SDS1104X-U,
calculates

```text
(CH2 + CH4) - (CH1 + CH3)
```

in volts, displays the result on a live Matplotlib graph, and streams the
aligned individual channel samples to CSV.

## Run

Connect the oscilloscope over USB and run:

```sh
uv run python main.py
```

The viewer automatically enables all four channel traces while it runs because
all four are required by the expression. Closing the graph restores their
original display states and trigger mode. It temporarily uses AUTO triggering
so live data is available even if the scope was initially stopped.

Each run creates a timestamped file such as
`siglent_capture_20260928_153000.csv` in the current directory. Every row
contains the frame number, UTC capture time, sample time, CH1 through CH4 in
volts, the calculated differential, and the acquisition metadata. The file is
flushed after every frame so completed frames remain available if acquisition
is interrupted.

Each update allows time for a fresh AUTO-mode acquisition and reads the four
current channel buffers in immediate succession. Firmware `3.2.1.1.5R6` does
not provide waveform data while acquisition is stopped, so the channel reads
are adjacent rather than an atomic stopped snapshot. The plot shows the entire
acquisition window. It uses `SANU?` to choose a transfer spacing that reduces a
large scope record to 2,000 plotted samples rather than transferring millions
of points on every update.

Useful tuning options:

```sh
# More plotted samples, with slower transfers
uv run python main.py --points 5000

# Faster refresh when the signal and USB connection are stable
uv run python main.py --acquisition-interval 0.1

# LAN instead of USB
uv run python main.py --resource TCPIP0::<scope-ip>::INSTR

# Choose the CSV filename
uv run python main.py --csv captures/measurement.csv
```

## VCA settling capture

Use trend mode for a VCA step test. It stores one compact row per acquired
frame instead of thousands of waveform samples, and plots both the differential
mean and the four-channel total against elapsed time:

```sh
uv run python main.py --trend --duration 180 --points 500 \
  --acquisition-interval 0.2 --time-div 0.01 --vca-current-ma 100
```

This gives 60 seconds for an initial baseline, about 60 seconds with the VCA
on, and 60 seconds after turn-off. Click the plot and press `1` immediately
after enabling the VCA, then press `0` immediately after disabling it. The
marker is attached to the next completed frame as `vca_on_100mA` or `vca_off`.

`--time-div 0.01` temporarily selects 10 ms/division, so each scope acquisition
spans roughly 140 ms. The scope chooses the corresponding sample rate. The
script restores the original timebase, channel display states, and trigger mode
when it exits. The acquisition is still a series of scope records transferred
over USB, not a mathematically gap-free stream; `elapsed_s` records the true
cadence so settling fits use real timestamps. When `--time-div` is supplied,
the logger waits for at least one complete 14-division record before requesting
the next transfer.

Trend CSV rows include each channel mean, differential mean/standard deviation/
range, the four-channel total, waveform span, transferred-point count, and
scope acquisition metadata. A three-minute trend file is only a few hundred
rows instead of tens of megabytes.

The default USB resource is:

```text
USB0::62700::4114::SDSAHBAQ800500::0::INSTR
```

The script verifies the SDS1104X-U model and serial number before acquisition.
`--allow-other-device` disables that check.

## Tests

```sh
uv run python -m unittest -v
```
