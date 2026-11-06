import unittest

from calc import chunk, median, parse_duration, slugify


class CalcTest(unittest.TestCase):
    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_parse_duration(self):
        self.assertEqual(parse_duration("1h30m15s"), 5415)
        self.assertEqual(parse_duration("45m"), 2700)
        self.assertEqual(parse_duration("2h"), 7200)

    def test_slugify(self):
        self.assertEqual(slugify("  Hello, World!  "), "hello-world")
        self.assertEqual(slugify("Router A/B test"), "router-a-b-test")

    def test_chunk(self):
        self.assertEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])


if __name__ == "__main__":
    unittest.main()
