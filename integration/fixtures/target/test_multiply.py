import unittest

from multiply import multiply


class MultiplyTest(unittest.TestCase):
    def test_multiply(self):
        self.assertEqual(multiply(6, 7), 42)


if __name__ == "__main__":
    unittest.main()
