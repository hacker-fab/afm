# Siglent live differential viewer

`main.py` repeatedly acquires coherent four-channel frames from the SDS1104X-U,
calculates

```text
(CH2 + CH4) - (CH1 + CH3)
```

in volts, and displays the result on a live Matplotlib graph.

## Run

Connect the oscilloscope over USB and run:

```sh
uv run python main.py
```

The viewer automatically enables all four channel traces while it runs because
all four are required by the expression. Closing the graph restores their
original display states and trigger mode. It temporarily uses AUTO triggering
so live data is available even if the scope was initially stopped.

Each update arms the scope, allows a fresh acquisition, and reads the four
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
```

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
