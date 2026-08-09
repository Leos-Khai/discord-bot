import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from seek_request import SeekRequest, parse_seek_request


class AbsoluteSeekRequestTests(unittest.TestCase):
    def test_reads_a_bare_number_as_a_count_of_seconds(self):
        self.assertEqual(SeekRequest(78, relative=False), parse_seek_request("78"))

    def test_reads_two_components_as_minutes_and_seconds(self):
        self.assertEqual(SeekRequest(60, relative=False), parse_seek_request("1:00"))

    def test_reads_three_components_as_hours_minutes_and_seconds(self):
        self.assertEqual(SeekRequest(3600, relative=False), parse_seek_request("1:00:00"))

    def test_reads_a_single_digit_seconds_component_as_written(self):
        self.assertEqual(SeekRequest(62, relative=False), parse_seek_request("1:2"))

    def test_allows_hours_beyond_a_single_day(self):
        self.assertEqual(SeekRequest(360000, relative=False), parse_seek_request("100:00:00"))


class RelativeSeekRequestTests(unittest.TestCase):
    def test_reads_a_leading_plus_as_a_shift_forward(self):
        self.assertEqual(SeekRequest(5, relative=True), parse_seek_request("+5"))

    def test_reads_a_leading_minus_as_a_shift_backward(self):
        self.assertEqual(SeekRequest(-30, relative=True), parse_seek_request("-30"))

    def test_applies_the_sign_to_every_clock_form(self):
        self.assertEqual(SeekRequest(60, relative=True), parse_seek_request("+1:00"))
        self.assertEqual(SeekRequest(-3600, relative=True), parse_seek_request("-1:00:00"))

    def test_keeps_a_signed_zero_relative_so_it_reports_the_current_position(self):
        self.assertEqual(SeekRequest(0, relative=True), parse_seek_request("+0"))
        self.assertEqual(SeekRequest(0, relative=True), parse_seek_request("-0"))

    def test_distinguishes_an_unsigned_value_from_a_forward_shift(self):
        self.assertEqual(SeekRequest(30, relative=False), parse_seek_request("30"))
        self.assertEqual(SeekRequest(30, relative=True), parse_seek_request("+30"))


class MalformedSeekRequestTests(unittest.TestCase):
    def test_rejects_a_seconds_component_that_overflows_a_minute(self):
        self.assertIsNone(parse_seek_request("1:70"))
        self.assertIsNone(parse_seek_request("1:00:60"))

    def test_rejects_a_minutes_component_that_overflows_an_hour(self):
        self.assertIsNone(parse_seek_request("90:00"))
        self.assertIsNone(parse_seek_request("1:60:00"))

    def test_rejects_fractional_seconds(self):
        self.assertIsNone(parse_seek_request("1.5"))
        self.assertIsNone(parse_seek_request("+2.5"))

    def test_rejects_more_components_than_a_clock_has(self):
        self.assertIsNone(parse_seek_request("1:00:00:00"))

    def test_rejects_a_sign_without_a_value(self):
        self.assertIsNone(parse_seek_request("+"))
        self.assertIsNone(parse_seek_request("-"))

    def test_rejects_a_sign_detached_from_its_value(self):
        self.assertIsNone(parse_seek_request("+ 5"))

    def test_rejects_a_doubled_sign(self):
        self.assertIsNone(parse_seek_request("--5"))
        self.assertIsNone(parse_seek_request("+-5"))

    def test_rejects_suffix_notation(self):
        self.assertIsNone(parse_seek_request("90s"))
        self.assertIsNone(parse_seek_request("1m30s"))

    def test_rejects_empty_and_blank_input(self):
        self.assertIsNone(parse_seek_request(""))
        self.assertIsNone(parse_seek_request("   "))

    def test_rejects_an_empty_clock_component(self):
        self.assertIsNone(parse_seek_request(":30"))
        self.assertIsNone(parse_seek_request("1:"))

    def test_ignores_whitespace_surrounding_an_otherwise_valid_value(self):
        self.assertEqual(SeekRequest(78, relative=False), parse_seek_request("  78  "))
        self.assertEqual(SeekRequest(-30, relative=True), parse_seek_request("-30\n"))
