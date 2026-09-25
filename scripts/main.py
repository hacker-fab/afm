"""Live four-channel differential viewer for a Siglent SDS1104X-U."""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
from dataclasses import dataclass

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
    result_v: npt.NDArray[np.float64]
    identity: str
    trigger_mode: str
    sample_rate: float
    acquired_points: int
    transfer_spacing: int
    sequence: int
    captured_at: float


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

    frame_start = trigger_delay - (14.0 * time_per_div / 2.0)
    time_axis = frame_start + np.arange(result.size, dtype=np.float64) * (
        spacing / sample_rate
    )
    return LiveFrame(
        time_s=time_axis,
        result_v=result,
        identity=identity,
        trigger_mode=trigger_mode,
        sample_rate=sample_rate,
        acquired_points=acquired_points,
        transfer_spacing=spacing,
        sequence=sequence,
        captured_at=time.monotonic(),
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
    initial_trigger_mode: str | None = None
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

        # Keep PyUSB and Matplotlib on the main thread. The SDS1104X-U's
        # USBTMC endpoint becomes unreliable when pyvisa-py I/O is moved to a
        # worker thread.
        time.sleep(args.acquisition_interval)
        frame = capture_frame(scope, identity, trigger_mode, args.points, 1)

        import matplotlib.pyplot as plt

        plt.ion()
        fig, axis = plt.subplots(figsize=(11, 6))
        (line,) = axis.plot([], [], color="#2563eb", linewidth=1.25)
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
        fig.canvas.manager.set_window_title("Siglent live differential")
        axis.set_title("Live: (CH2 + CH4) - (CH1 + CH3)")
        plt.tight_layout()
        previous_capture = frame.captured_at

        while plt.fignum_exists(fig.number):
            elapsed = frame.captured_at - previous_capture
            update_rate = 1.0 / elapsed if elapsed > 0 and frame.sequence > 1 else 0.0
            previous_capture = frame.captured_at
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

            time.sleep(args.acquisition_interval)
            frame = capture_frame(
                scope, identity, trigger_mode, args.points, frame.sequence + 1
            )
    finally:
        if scope is not None:
            try:
                scope.write("STOP")
                for channel, state in initial_trace_states.items():
                    scope.write(f"C{channel}:TRA {state}")
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


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate_args(args)
        graph_live(args)
    except (ValueError, RuntimeError, OSError, pyvisa.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
