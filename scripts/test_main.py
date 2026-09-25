import unittest

import numpy as np

from main import combine_channels, parse_data_block, parse_quantity, signed_codes


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
    def test_requested_four_channel_expression(self):
        channels = {
            1: np.array([1.0, 2.0, 3.0]),
            2: np.array([10.0, 20.0, 30.0]),
            3: np.array([4.0, 5.0, 6.0]),
            4: np.array([40.0, 50.0, 60.0]),
        }
        np.testing.assert_array_equal(
            combine_channels(channels),
            np.array([45.0, 63.0, 81.0]),
        )

    def test_aligns_to_shortest_channel(self):
        channels = {
            1: np.array([1.0, 2.0]),
            2: np.array([10.0, 20.0, 30.0]),
            3: np.array([4.0, 5.0, 6.0]),
            4: np.array([40.0, 50.0, 60.0]),
        }
        np.testing.assert_array_equal(combine_channels(channels), np.array([45.0, 63.0]))


if __name__ == "__main__":
    unittest.main()
