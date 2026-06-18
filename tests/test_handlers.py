"""Unit tests for the subnet-watcher Lambda IP-math and flagging logic.

These tests use only the standard library (unittest + mock) and never touch
AWS: the CloudWatch metric publishing call is patched out.
"""
import os
import sys
import unittest
from unittest import mock

# handlers.py reads these environment variables at import time, so they must
# exist before the module is imported.
os.environ.setdefault("PERCENTAGE_REMAINING_WARNING", "20")
os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:eu-west-1:123456789012:test")
os.environ.setdefault("MESSAGE_SUBJECT", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import handlers  # noqa: E402  (import must follow the env/path setup above)


class FakeSubnet:
    """Minimal stand-in for a boto3 ec2.Subnet resource."""

    def __init__(self, subnet_id, cidr_block, available):
        self.id = subnet_id
        self.cidr_block = cidr_block
        self.available_ip_address_count = available


class CheckForLowIpsTests(unittest.TestCase):
    def setUp(self):
        # Prevent any real CloudWatch PutMetricData calls.
        patcher = mock.patch.object(handlers, "put_subnet_metrics")
        self.addCleanup(patcher.stop)
        self.mock_put = patcher.start()

    def _percent_published(self):
        # put_subnet_metrics(subnet_id, vpc, available_ips, total_ips, percent)
        return self.mock_put.call_args.args[4]

    def _total_published(self):
        return self.mock_put.call_args.args[3]

    def test_total_ips_subtracts_five_reserved(self):
        # /24 = 256 addresses, minus the 5 AWS-reserved = 251 usable.
        subnet = FakeSubnet("subnet-1", "10.0.0.0/24", available=251)
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips([subnet], "vpc-1", "eu-west-1")
        self.assertEqual(flagged, [])
        self.assertEqual(self._total_published(), 251)
        self.assertEqual(self._percent_published(), 100.0)

    def test_percent_precision_is_not_truncated(self):
        # 25 / 251 * 100 = 9.96%. The old code rounded before multiplying and
        # would have produced 0.1 * 100 = 10.0; this guards the fix.
        subnet = FakeSubnet("subnet-2", "10.0.0.0/24", available=25)
        with mock.patch.object(handlers, "percent_warning", 5):
            flagged = handlers.check_for_low_ips([subnet], "vpc-1", "eu-west-1")
        self.assertEqual(flagged, [])
        self.assertEqual(self._percent_published(), 9.96)

    def test_low_ip_subnet_is_flagged(self):
        subnet = FakeSubnet("subnet-3", "10.0.0.0/24", available=10)  # 3.98%
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips([subnet], "vpc-1", "eu-west-1")
        self.assertEqual(len(flagged), 1)
        subnet_id, vpc, region, percent = flagged[0]
        self.assertEqual(subnet_id, "subnet-3")
        self.assertEqual(vpc, "vpc-1")
        self.assertEqual(region, "eu-west-1")
        self.assertLessEqual(percent, 20)

    def test_threshold_boundary_is_inclusive(self):
        # percent_remaining == threshold must flag (comparison is <=).
        subnet = FakeSubnet("subnet-4", "10.0.0.0/24", available=25)  # 9.96%
        with mock.patch.object(handlers, "percent_warning", 9.96):
            flagged = handlers.check_for_low_ips([subnet], "vpc-1", "eu-west-1")
        self.assertEqual(len(flagged), 1)

    def test_tiny_cidr_is_skipped_without_crashing(self):
        # /30 = 4 addresses - 5 reserved = -1 usable -> skipped, no metric, no
        # ZeroDivisionError.
        subnet = FakeSubnet("subnet-5", "10.0.0.0/30", available=0)
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips([subnet], "vpc-1", "eu-west-1")
        self.assertEqual(flagged, [])
        self.mock_put.assert_not_called()

    def test_mixed_subnets_flags_only_low_ones(self):
        subnets = [
            FakeSubnet("ok", "10.0.0.0/24", available=251),   # 100%
            FakeSubnet("low", "10.0.1.0/24", available=5),    # 1.99%
        ]
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips(subnets, "vpc-1", "eu-west-1")
        self.assertEqual([f[0] for f in flagged], ["low"])


if __name__ == "__main__":
    unittest.main()
