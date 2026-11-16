import csv
import io
import unittest
from argparse import Namespace

import numpy as np

from main import (
    CSV_COLUMNS,
    TREND_CSV_COLUMNS,
    LiveFrame,
    acquire_single,
    acquire_stop,
    acquisition_wait_seconds,
    combine_channels,
    parse_data_block,
    parse_quantity,
    signed_codes,
    trend_row,
    write_frame_csv,
    write_trend_csv,
)


class ParseQuantityTests(unittest.TestCase):
    def test_scientific_voltage(self):
        self.assertEqual(parse_quantity("C1:VDIV 5.00E-01V"), 0.5)

    def test_prefixed_sample_rate(self):
        self.assertEqual(parse_quantity("SARA 1.00GSa/s"), 1e9)

    def test_prefixed_time(self):
        self.assertEqual(parse_quantity("TRDL -5.000000ns"), -5e-9)

    def test_acquired_point_count(self):
        self.assertEqual(parse_quantity("SANU 7.00E+05pts"), 700_000)


class WaveformBlockTests(unittest.TestCase):
    def test_extracts_siglent_block_without_treating_newline_as_termination(self):
        payload = bytes((2, 3, 10, 255, 128))
        response = b"C1:WF DAT2,#9000000005" + payload + b"\n\n"
        self.assertEqual(parse_data_block(response), payload)

    def test_rejects_truncated_payload(self):
        with self.assertRaisesRegex(ValueError, "promised 5 bytes"):
            parse_data_block(b"C1:WF DAT2,#15abc")

    def test_converts_unsigned_wire_bytes_to_signed_codes(self):
        self.assertEqual(signed_codes(bytes((0, 127, 128, 255))), (0, 127, -128, -1))


class ChannelMathTests(unittest.TestCase):
    def test_focus_error_is_a_plus_c_minus_b_minus_d(self):
        channels = {
            1: np.array([1.0, 2.0, 3.0]),
            2: np.array([10.0, 20.0, 30.0]),
            3: np.array([4.0, 5.0, 6.0]),
            4: np.array([40.0, 50.0, 60.0]),
        }
        np.testing.assert_array_equal(
            combine_channels(channels),
            np.array([-45.0, -63.0, -81.0]),
        )

    def test_aligns_to_shortest_channel(self):
        channels = {
            1: np.array([1.0, 2.0]),
            2: np.array([10.0, 20.0, 30.0]),
            3: np.array([4.0, 5.0, 6.0]),
            4: np.array([40.0, 50.0, 60.0]),
        }
        np.testing.assert_array_equal(combine_channels(channels), np.array([-45.0, -63.0]))


class CsvLoggingTests(unittest.TestCase):
    @staticmethod
    def make_frame():
        return LiveFrame(
            time_s=np.array([-1e-6, 0.0]),
            channels_v={
                1: np.array([1.0, 2.0]),
                2: np.array([10.0, 20.0]),
                3: np.array([4.0, 5.0]),
                4: np.array([40.0, 50.0]),
            },
            result_v=np.array([45.0, 63.0]),
            identity="Siglent,SDS1104X-U,SDSAHBAQ800500,1.0",
            trigger_mode="AUTO",
            sample_rate=1e9,
            acquired_points=700_000,
            transfer_spacing=350,
            sequence=7,
            captured_at=12.5,
            captured_at_utc="2026-09-28T20:00:00+00:00",
        )

    def test_writes_aligned_individual_channels_and_result(self):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(CSV_COLUMNS)
        frame = self.make_frame()

        write_frame_csv(writer, frame)

        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["frame"], "7")
        self.assertEqual(rows[0]["sample_index"], "0")
        self.assertEqual(rows[0]["a_v"], "1.0")
        self.assertEqual(rows[0]["d_v"], "40.0")
        self.assertEqual(rows[0]["focus_error_v"], "45.0")
        self.assertEqual(rows[1]["time_s"], "0.0")

    def test_reduces_frame_to_one_settling_trend_row(self):
        frame = self.make_frame()
        row = trend_row(frame, started_at=10.0, event="vca_on_100mA")

        values = dict(zip(TREND_CSV_COLUMNS, row, strict=True))
        self.assertEqual(values["frame"], 7)
        self.assertEqual(values["elapsed_s"], 2.5)
        self.assertEqual(values["event"], "vca_on_100mA")
        self.assertEqual(values["a_mean_v"], 1.5)
        self.assertEqual(values["d_mean_v"], 45.0)
        self.assertEqual(values["focus_error_mean_v"], 54.0)
        self.assertEqual(values["focus_error_std_v"], 9.0)
        self.assertEqual(values["total_mean_v"], 66.0)
        self.assertEqual(values["transferred_points"], 2)
        self.assertAlmostEqual(values["waveform_span_s"], 1e-6)

    def test_writes_one_csv_line_per_trend_frame(self):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(TREND_CSV_COLUMNS)

        write_trend_csv(writer, self.make_frame(), 10.0, "vca_off")

        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "vca_off")
        self.assertEqual(rows[0]["focus_error_mean_v"], "54.0")


class FakeScope:
    def __init__(self, trmd_replies):
        self.written = []
        self._replies = list(trmd_replies)
        self._pending = b""

    def write(self, command):
        self.written.append(command)
        if command == "TRMD?":
            # The last reply repeats once the script is exhausted.
            reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
            self._pending = f"TRMD {reply}".encode()

    def read_raw(self):
        return self._pending


class AcquireTests(unittest.TestCase):
    def test_stop_mode_runs_auto_then_stops(self):
        scope = FakeScope(["AUTO", "STOP"])
        acquire_stop(scope, run_s=0.0)
        self.assertEqual(scope.written[:2], ["TRMD AUTO", "STOP"])
        self.assertIn("TRMD?", scope.written)

    def test_single_mode_waits_for_stop(self):
        scope = FakeScope(["SINGLE", "SINGLE", "STOP"])
        acquire_single(scope, timeout_s=5.0)
        self.assertEqual(scope.written[0], "TRMD SINGLE")
        self.assertNotIn("FRTR", scope.written)

    def test_single_mode_explains_trigger_timeout(self):
        scope = FakeScope(["SINGLE"])
        with self.assertRaisesRegex(RuntimeError, "trigger never fired"):
            acquire_single(scope, timeout_s=0.0)


class AcquisitionTimingTests(unittest.TestCase):
    def test_uses_requested_interval_without_timebase_override(self):
        args = Namespace(acquisition_interval=0.2, time_div=None)
        self.assertEqual(acquisition_wait_seconds(args), 0.2)

    def test_waits_for_complete_fourteen_division_record(self):
        args = Namespace(acquisition_interval=0.1, time_div=0.01)
        self.assertAlmostEqual(acquisition_wait_seconds(args), 0.147)


if __name__ == "__main__":
    unittest.main()
