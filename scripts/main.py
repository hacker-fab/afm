"""Live four-channel differential viewer for a Siglent SDS1104X-U."""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyvisa
from pyvisa.resources import MessageBasedResource


DEFAULT_RESOURCE = "USB0::62700::4114::SDSAHBAQ800500::0::INSTR"
EXPECTED_MODEL = "SDS1104X-U"
EXPECTED_SERIAL = "SDSAHBAQ800500"

_NUMBER_WITH_UNIT = re.compile(
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)\s*([A-Za-z/]*)"
)
_SI_MULTIPLIERS = {
    "": 1.0,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}


@dataclass(frozen=True)
class LiveFrame:
    time_s: npt.NDArray[np.float64]
    channels_v: dict[int, npt.NDArray[np.float64]]
    result_v: npt.NDArray[np.float64]
    identity: str
    trigger_mode: str
    sample_rate: float
    acquired_points: int
    transfer_spacing: int
    sequence: int
    captured_at: float
    captured_at_utc: str


CSV_COLUMNS = (
    "frame",
    "captured_at_utc",
    "sample_index",
    "time_s",
    "ch1_v",
    "ch2_v",
    "ch3_v",
    "ch4_v",
    "differential_v",
    "sample_rate_sa_s",
    "acquired_points",
    "transfer_spacing",
    "trigger_mode",
)

TREND_CSV_COLUMNS = (
    "frame",
    "captured_at_utc",
    "elapsed_s",
    "event",
    "ch1_mean_v",
    "ch2_mean_v",
    "ch3_mean_v",
    "ch4_mean_v",
    "differential_mean_v",
    "differential_std_v",
    "differential_min_v",
    "differential_max_v",
    "total_mean_v",
    "sample_rate_sa_s",
    "acquired_points",
    "transferred_points",
    "transfer_spacing",
    "waveform_span_s",
    "trigger_mode",
)


def parse_quantity(response: str) -> float:
    """Parse a Siglent response such as ``SARA 1.00GSa/s`` into SI units."""
    value_text = response.partition(" ")[2].strip()
    match = _NUMBER_WITH_UNIT.match(value_text)
    if match is None:
        raise ValueError(f"Could not parse numeric response: {response!r}")
    value, unit = match.groups()
    prefix = ""
    if unit.casefold() != "pts" and unit[:1] in _SI_MULTIPLIERS:
        prefix = unit[0]
    return float(value) * _SI_MULTIPLIERS[prefix]


def parse_data_block(response: bytes) -> bytes:
    """Extract the waveform bytes from a Siglent ``WF? DAT2`` response."""
    marker = response.find(b"#")
    if marker < 0 or marker + 1 >= len(response):
        raise ValueError("Waveform reply has no binary-block header")

    digit_count_byte = response[marker + 1 : marker + 2]
    if not digit_count_byte.isdigit():
        raise ValueError("Waveform reply has an invalid binary-block header")
    digit_count = int(digit_count_byte)
    length_start = marker + 2
    length_end = length_start + digit_count
    if length_end > len(response):
        raise ValueError("Waveform reply ends inside its length field")

    try:
        data_length = int(response[length_start:length_end])
    except ValueError as exc:
        raise ValueError("Waveform reply has an invalid data length") from exc

    data_end = length_end + data_length
    if data_end > len(response):
        raise ValueError(
            f"Waveform reply promised {data_length} bytes but only "
            f"{len(response) - length_end} arrived"
        )
    return response[length_end:data_end]


def signed_codes(data: bytes) -> tuple[int, ...]:
    """Return signed ADC codes; retained as a small, dependency-light helper."""
    return tuple(value if value < 128 else value - 256 for value in data)


def response_value(response: str) -> str:
    return response.partition(" ")[2].strip()


def query_text(scope: MessageBasedResource, command: str) -> str:
    # Avoid PyVISA's line-termination pending buffer. This firmware sometimes
    # makes a binary waveform ready just after an LF-terminated text response;
    # raw, USBTMC-message-bounded reads keep the two reply types synchronized.
    scope.write(command)
    response = scope.read_raw()
    try:
        return response.decode("ascii").strip()
    except UnicodeDecodeError:
        if b":WF " not in response[:24]:
            raise
        # A reply from an earlier asynchronous WF? can surface first and the
        # scope drops this text query. Retry it after consuming that block.
        time.sleep(0.05)
        scope.write(command)
        return scope.read_raw().decode("ascii").strip()


def read_waveform_bytes(scope: MessageBasedResource, channel: int) -> bytes:
    # Waveform bytes can equal LF, so only the USBTMC end-of-message marker may
    # terminate this read. Treating LF as termination silently truncates frames.
    previous_termination = scope.read_termination
    try:
        scope.read_termination = None
        command = f"C{channel}:WF? DAT2"
        scope.write(command)
        data = parse_data_block(scope.read_raw())
        if not data:
            # Firmware 3.2.1.1.5R6 can return a zero-length block while it
            # prepares the waveform. Retry after the setup has had more time.
            time.sleep(0.1)
            scope.write(command)
            data = parse_data_block(scope.read_raw())
        return data
    finally:
        scope.read_termination = previous_termination


def read_channel_volts(
    scope: MessageBasedResource,
    channel: int,
    volts_per_div: float,
    offset: float,
) -> npt.NDArray[np.float64]:
    codes = np.frombuffer(read_waveform_bytes(scope, channel), dtype=np.int8)
    return codes.astype(np.float64) * (volts_per_div / 25.0) - offset


def combine_channels(
    channels: dict[int, npt.NDArray[np.float64]],
) -> npt.NDArray[np.float64]:
    """Compute ``(CH2 + CH4) - (CH1 + CH3)`` on aligned samples."""
    missing = set(range(1, 5)) - channels.keys()
    if missing:
        raise ValueError(f"Missing channel data: {sorted(missing)}")
    point_count = min(len(values) for values in channels.values())
    if point_count == 0:
        lengths = ", ".join(
            f"CH{channel}={len(values)}" for channel, values in channels.items()
        )
        raise ValueError(f"The scope returned an empty waveform ({lengths})")
    return (
        channels[2][:point_count]
        + channels[4][:point_count]
        - channels[1][:point_count]
        - channels[3][:point_count]
    )


def write_frame_csv(writer: Any, frame: LiveFrame) -> None:
    """Append one aligned four-channel frame to an open CSV writer."""
    writer.writerows(
        (
            frame.sequence,
            frame.captured_at_utc,
            sample_index,
            frame.time_s[sample_index],
            frame.channels_v[1][sample_index],
            frame.channels_v[2][sample_index],
            frame.channels_v[3][sample_index],
            frame.channels_v[4][sample_index],
            frame.result_v[sample_index],
            frame.sample_rate,
            frame.acquired_points,
            frame.transfer_spacing,
            frame.trigger_mode,
        )
        for sample_index in range(frame.result_v.size)
    )


def trend_row(
    frame: LiveFrame,
    started_at: float,
    event: str = "",
) -> tuple[Any, ...]:
    """Reduce one waveform frame to a compact settling-trend record."""
    channel_means = tuple(
        float(np.mean(frame.channels_v[channel])) for channel in range(1, 5)
    )
    waveform_span = (
        float(frame.time_s[-1] - frame.time_s[0]) if frame.time_s.size > 1 else 0.0
    )
    return (
        frame.sequence,
        frame.captured_at_utc,
        frame.captured_at - started_at,
        event,
        *channel_means,
        float(np.mean(frame.result_v)),
        float(np.std(frame.result_v)),
        float(np.min(frame.result_v)),
        float(np.max(frame.result_v)),
        sum(channel_means),
        frame.sample_rate,
        frame.acquired_points,
        frame.result_v.size,
        frame.transfer_spacing,
        waveform_span,
        frame.trigger_mode,
    )


def write_trend_csv(
    writer: Any,
    frame: LiveFrame,
    started_at: float,
    event: str = "",
) -> tuple[Any, ...]:
    """Append and return one compact settling-trend record."""
    row = trend_row(frame, started_at, event)
    writer.writerow(row)
    return row


def acquisition_wait_seconds(args: argparse.Namespace) -> float:
    """Allow at least one complete timebase record before each transfer."""
    if args.time_div is None:
        return args.acquisition_interval
    return max(args.acquisition_interval, 14.0 * args.time_div * 1.05)


def capture_frame(
    scope: MessageBasedResource,
    identity: str,
    trigger_mode: str,
    requested_points: int,
    sequence: int,
) -> LiveFrame:
    """Transfer the scope's current four-channel acquisition buffer."""
    sample_rate = parse_quantity(query_text(scope, "SARA?"))
    time_per_div = parse_quantity(query_text(scope, "TDIV?"))
    trigger_delay = parse_quantity(query_text(scope, "TRDL?"))
    acquired_points = round(parse_quantity(query_text(scope, "SANU? C1")))
    if sample_rate <= 0 or acquired_points <= 0:
        raise ValueError(
            f"Invalid acquisition geometry: {sample_rate=} {acquired_points=}"
        )

    channel_scales = {
        channel: (
            parse_quantity(query_text(scope, f"C{channel}:VDIV?")),
            parse_quantity(query_text(scope, f"C{channel}:OFST?")),
        )
        for channel in range(1, 5)
    }

    # SP=1 means every point. Larger SP values decimate the complete record,
    # keeping the live transfer bounded while retaining the full time span.
    spacing = max(1, math.ceil(acquired_points / requested_points))
    # On firmware 3.2.1.1.5R6, NP is the raw input window *before* SP is
    # applied. NP=acquired_points plus SP=N therefore returns roughly
    # acquired_points/N values spanning the full record.
    scope.write(f"WFSU SP,{spacing},NP,{acquired_points},FP,0")
    time.sleep(0.2)
    channels: dict[int, npt.NDArray[np.float64]] = {}
    for channel in range(1, 5):
        try:
            channels[channel] = read_channel_volts(
                scope, channel, *channel_scales[channel]
            )
        except (OSError, ValueError, pyvisa.Error) as exc:
            raise RuntimeError(f"Failed while reading CH{channel}: {exc}") from exc
    result = combine_channels(channels)
    channels = {
        channel: values[: result.size] for channel, values in channels.items()
    }

    frame_start = trigger_delay - (14.0 * time_per_div / 2.0)
    time_axis = frame_start + np.arange(result.size, dtype=np.float64) * (
        spacing / sample_rate
    )
    return LiveFrame(
        time_s=time_axis,
        channels_v=channels,
        result_v=result,
        identity=identity,
        trigger_mode=trigger_mode,
        sample_rate=sample_rate,
        acquired_points=acquired_points,
        transfer_spacing=spacing,
        sequence=sequence,
        captured_at=time.monotonic(),
        captured_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def validate_identity(identity: str, allow_other_device: bool) -> None:
    parts = [part.strip() for part in identity.split(",")]
    if not allow_other_device and (
        len(parts) < 3
        or parts[1] != EXPECTED_MODEL
        or parts[2] != EXPECTED_SERIAL
    ):
        raise RuntimeError(
            f"Refusing unexpected instrument {identity!r}; expected "
            f"{EXPECTED_MODEL}, serial {EXPECTED_SERIAL}"
        )


def graph_live(args: argparse.Namespace) -> None:
    resource_manager = pyvisa.ResourceManager("@py")
    scope: MessageBasedResource | None = None
    csv_file = None
    initial_trigger_mode: str | None = None
    initial_time_division: str | None = None
    initial_trace_states: dict[int, str] = {}
    try:
        scope = resource_manager.open_resource(args.resource)
        scope.timeout = round(args.timeout * 1000)
        scope.chunk_size = 1024 * 1024
        scope.write_termination = "\n"
        scope.read_termination = None

        identity = query_text(scope, "*IDN?")
        validate_identity(identity, args.allow_other_device)
        trigger_mode = response_value(query_text(scope, "TRMD?"))
        initial_trigger_mode = trigger_mode
        initial_time_division = response_value(query_text(scope, "TDIV?"))

        # All four traces must exist for the requested expression. Remember and
        # restore the display states because enabling a channel changes the
        # SDS1104X-U's sample-rate/memory allocation.
        for channel in range(1, 5):
            state = response_value(query_text(scope, f"C{channel}:TRA?")).upper()
            initial_trace_states[channel] = state
            if state != "ON":
                scope.write(f"C{channel}:TRA ON")

        # AUTO guarantees that a current buffer exists even if the user's
        # original mode was STOP or a NORMAL/SINGLE trigger is not firing.
        scope.write("TRMD AUTO")
        if args.time_div is not None:
            scope.write(f"TDIV {args.time_div:.12g}S")
            requested_time_div = args.time_div
            args.time_div = parse_quantity(query_text(scope, "TDIV?"))
            if not math.isclose(args.time_div, requested_time_div, rel_tol=0.01):
                print(
                    "Scope adapted --time-div from "
                    f"{requested_time_div:g} to {args.time_div:g} s/div"
                )

        # Keep PyUSB and Matplotlib on the main thread. The SDS1104X-U's
        # USBTMC endpoint becomes unreliable when pyvisa-py I/O is moved to a
        # worker thread.
        time.sleep(acquisition_wait_seconds(args))
        frame = capture_frame(scope, identity, trigger_mode, args.points, 1)

        csv_path = args.csv
        if csv_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            prefix = "siglent_trend" if args.trend else "siglent_capture"
            csv_path = Path(f"{prefix}_{timestamp}.csv")
        csv_path = csv_path.expanduser().resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_file = csv_path.open("w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(TREND_CSV_COLUMNS if args.trend else CSV_COLUMNS)
        started_at = frame.captured_at
        if args.trend:
            first_trend_row = write_trend_csv(csv_writer, frame, started_at)
        else:
            first_trend_row = None
            write_frame_csv(csv_writer, frame)
        csv_file.flush()
        output_kind = "settling trend" if args.trend else "waveform samples"
        print(f"Writing {output_kind} to {csv_path}")

        import matplotlib.pyplot as plt

        plt.ion()
        if args.trend:
            fig, (axis, total_axis) = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
            (line,) = axis.plot([], [], color="#2563eb", linewidth=1.25)
            (total_line,) = total_axis.plot([], [], color="#dc2626", linewidth=1.25)
            axis.set_ylabel("Differential mean (mV)")
            total_axis.set_ylabel("CH1 + CH2 + CH3 + CH4 mean (mV)")
            total_axis.set_xlabel("Elapsed time (s)")
            for plot_axis in (axis, total_axis):
                plot_axis.grid(True, alpha=0.25)
            trend_elapsed = [float(first_trend_row[2])]
            trend_differential_mv = [float(first_trend_row[8]) * 1000.0]
            trend_total_mv = [float(first_trend_row[12]) * 1000.0]
            pending_events: list[str] = []

            def mark_vca(event: Any) -> None:
                if event.key == "1":
                    label = f"vca_on_{args.vca_current_ma:g}mA"
                elif event.key == "0":
                    label = "vca_off"
                else:
                    return
                pending_events.append(label)
                print(f"Queued event marker for next frame: {label}")

            fig.canvas.mpl_connect("key_press_event", mark_vca)
        else:
            fig, axis = plt.subplots(figsize=(11, 6))
            total_axis = None
            total_line = None
            line, = axis.plot([], [], color="#2563eb", linewidth=1.25)
            axis.axhline(0.0, color="#777777", linewidth=0.8, alpha=0.7)
            axis.grid(True, alpha=0.25)
            axis.set_xlabel("Time relative to trigger (s)")
            axis.set_ylabel("(CH2 + CH4) - (CH1 + CH3) (V)")
        status_text = axis.text(
            0.01,
            0.99,
            "",
            transform=axis.transAxes,
            ha="left",
            va="top",
            fontsize=9,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.75},
        )
        window_title = (
            "Siglent settling trend" if args.trend else "Siglent live differential"
        )
        fig.canvas.manager.set_window_title(window_title)
        if args.trend:
            axis.set_title(
                "VCA settling trend: press 1 when current turns on, 0 when it turns off"
            )
        else:
            axis.set_title("Live: (CH2 + CH4) - (CH1 + CH3)")
        plt.tight_layout()
        previous_capture = frame.captured_at

        while plt.fignum_exists(fig.number):
            elapsed = frame.captured_at - previous_capture
            update_rate = 1.0 / elapsed if elapsed > 0 and frame.sequence > 1 else 0.0
            previous_capture = frame.captured_at
            if args.trend:
                line.set_data(trend_elapsed, trend_differential_mv)
                total_line.set_data(trend_elapsed, trend_total_mv)
                for plot_axis in (axis, total_axis):
                    plot_axis.relim()
                    plot_axis.autoscale_view()
                status_text.set_text(
                    f"Frame {frame.sequence} | {update_rate:.2f} updates/s | "
                    f"elapsed {trend_elapsed[-1]:.1f} s\n"
                    f"scope {frame.sample_rate / 1e6:.3g} MSa/s | "
                    f"{frame.result_v.size:,} transferred points/frame"
                )
            else:
                line.set_data(frame.time_s, frame.result_v)
                axis.relim()
                axis.autoscale_view()
                status_text.set_text(
                    f"Frame {frame.sequence} | {update_rate:.1f} updates/s | "
                    f"scope {frame.sample_rate / 1e6:.3g} MSa/s\n"
                    f"{frame.acquired_points:,} acquired points, every "
                    f"{frame.transfer_spacing:,}th point plotted | "
                    f"trigger {frame.trigger_mode}"
                )
            fig.canvas.draw_idle()
            fig.canvas.flush_events()
            plt.pause(args.refresh_ms / 1000.0)
            if not plt.fignum_exists(fig.number):
                break
            if (
                args.duration is not None
                and frame.captured_at - started_at >= args.duration
            ):
                break

            time.sleep(acquisition_wait_seconds(args))
            frame = capture_frame(
                scope, identity, trigger_mode, args.points, frame.sequence + 1
            )
            if args.trend:
                marker = pending_events.pop(0) if pending_events else ""
                row = write_trend_csv(csv_writer, frame, started_at, marker)
                trend_elapsed.append(float(row[2]))
                trend_differential_mv.append(float(row[8]) * 1000.0)
                trend_total_mv.append(float(row[12]) * 1000.0)
            else:
                write_frame_csv(csv_writer, frame)
            csv_file.flush()
    finally:
        if csv_file is not None:
            csv_file.close()
        if scope is not None:
            try:
                scope.write("STOP")
                for channel, state in initial_trace_states.items():
                    scope.write(f"C{channel}:TRA {state}")
                if initial_time_division is not None:
                    scope.write(f"TDIV {initial_time_division}")
                if initial_trigger_mode is not None:
                    scope.write(f"TRMD {initial_trigger_mode}")
            except (OSError, pyvisa.Error):
                pass
            scope.close()
        resource_manager.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Live-plot (CH2 + CH4) - (CH1 + CH3) from an SDS1104X-U."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help=(
            "CSV output path (default: a timestamped siglent_capture_*.csv, "
            "or siglent_trend_*.csv with --trend, in the current directory)"
        ),
    )
    parser.add_argument(
        "--trend",
        action="store_true",
        help=(
            "log and plot compact per-frame means for a settling test instead "
            "of writing every waveform sample"
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="stop after this many elapsed seconds (default: run until plot closes)",
    )
    parser.add_argument(
        "--time-div",
        type=float,
        help=(
            "temporarily set the scope timebase in seconds/division; the original "
            "setting is restored on exit"
        ),
    )
    parser.add_argument(
        "--vca-current-ma",
        type=float,
        default=100.0,
        help="current used in the trend plot event label (default: 100 mA)",
    )
    parser.add_argument(
        "--resource",
        default=DEFAULT_RESOURCE,
        help=f"VISA resource (default: {DEFAULT_RESOURCE})",
    )
    parser.add_argument(
        "-n",
        "--points",
        type=int,
        default=2000,
        help="points transferred per channel per update (default: 2000)",
    )
    parser.add_argument(
        "--acquisition-interval",
        type=float,
        default=0.5,
        help="seconds allowed for each fresh acquisition (default: 0.5)",
    )
    parser.add_argument(
        "--refresh-ms",
        type=int,
        default=33,
        help="plot polling interval in milliseconds (default: 33)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="instrument I/O timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--allow-other-device",
        action="store_true",
        help="skip the model and serial-number safety check",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.points < 2:
        raise ValueError("--points must be at least 2")
    if args.acquisition_interval <= 0:
        raise ValueError("--acquisition-interval must be positive")
    if args.refresh_ms < 1:
        raise ValueError("--refresh-ms must be at least 1")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.duration is not None and args.duration <= 0:
        raise ValueError("--duration must be positive")
    if args.time_div is not None and args.time_div <= 0:
        raise ValueError("--time-div must be positive")
    if args.vca_current_ma <= 0:
        raise ValueError("--vca-current-ma must be positive")


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate_args(args)
        graph_live(args)
    except (ValueError, RuntimeError, OSError, csv.Error, pyvisa.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
