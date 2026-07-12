import unittest

from sqmy.diagnostics import SimulatedQuotaClient
from sqmy.providers import QuotaExceeded


class DiagnosticsTest(unittest.TestCase):
    def test_simulated_quota_has_expected_error_code(self):
        with self.assertRaises(QuotaExceeded) as caught:
            SimulatedQuotaClient().analyze("x", {})
        self.assertEqual(caught.exception.code, "quota_exceeded")
