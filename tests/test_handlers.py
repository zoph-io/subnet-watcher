"""Unit tests for the subnet-watcher Lambda IP-math and flagging logic.

These tests use only the standard library (unittest + mock) and never touch
AWS: `check_for_low_ips` builds metric data into a list instead of calling
CloudWatch, so it can be exercised directly.
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


def metric_value(metric_data, name, subnet_id):
    for entry in metric_data:
        if entry["MetricName"] != name:
            continue
        dims = {d["Name"]: d["Value"] for d in entry["Dimensions"]}
        if dims.get("SubnetId") == subnet_id:
            return entry["Value"]
    raise AssertionError(f"metric {name} for {subnet_id} not found")


class CheckForLowIpsTests(unittest.TestCase):
    def test_total_ips_subtracts_five_reserved(self):
        # /24 = 256 addresses, minus the 5 AWS-reserved = 251 usable.
        subnet = FakeSubnet("subnet-1", "10.0.0.0/24", available=251)
        metric_data = []
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips(
                [subnet], "vpc-1", "eu-west-1", metric_data
            )
        self.assertEqual(flagged, [])
        self.assertEqual(metric_value(metric_data, "TotalIpAddressCount", "subnet-1"), 251)
        self.assertEqual(
            metric_value(metric_data, "AvailableIpAddressPercent", "subnet-1"), 100.0
        )

    def test_percent_precision_is_not_truncated(self):
        # 25 / 251 * 100 = 9.96%. The old code rounded before multiplying and
        # would have produced 0.1 * 100 = 10.0; this guards the fix.
        subnet = FakeSubnet("subnet-2", "10.0.0.0/24", available=25)
        metric_data = []
        with mock.patch.object(handlers, "percent_warning", 5):
            flagged = handlers.check_for_low_ips(
                [subnet], "vpc-1", "eu-west-1", metric_data
            )
        self.assertEqual(flagged, [])
        self.assertEqual(
            metric_value(metric_data, "AvailableIpAddressPercent", "subnet-2"), 9.96
        )

    def test_low_ip_subnet_is_flagged(self):
        subnet = FakeSubnet("subnet-3", "10.0.0.0/24", available=10)  # 3.98%
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips([subnet], "vpc-1", "eu-west-1", [])
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
            flagged = handlers.check_for_low_ips([subnet], "vpc-1", "eu-west-1", [])
        self.assertEqual(len(flagged), 1)

    def test_tiny_cidr_is_skipped_without_crashing(self):
        # /30 = 4 addresses - 5 reserved = -1 usable -> skipped, no metric, no
        # ZeroDivisionError.
        subnet = FakeSubnet("subnet-5", "10.0.0.0/30", available=0)
        metric_data = []
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips(
                [subnet], "vpc-1", "eu-west-1", metric_data
            )
        self.assertEqual(flagged, [])
        self.assertEqual(metric_data, [])

    def test_mixed_subnets_flags_only_low_ones(self):
        subnets = [
            FakeSubnet("ok", "10.0.0.0/24", available=251),   # 100%
            FakeSubnet("low", "10.0.1.0/24", available=5),    # 1.99%
        ]
        with mock.patch.object(handlers, "percent_warning", 20):
            flagged = handlers.check_for_low_ips(subnets, "vpc-1", "eu-west-1", [])
        self.assertEqual([f[0] for f in flagged], ["low"])

    def test_subnet_allow_list_filters_out_others(self):
        subnets = [
            FakeSubnet("keep", "10.0.0.0/24", available=5),   # low, but allowed
            FakeSubnet("ignore", "10.0.1.0/24", available=5),  # low, but filtered
        ]
        metric_data = []
        with mock.patch.object(handlers, "percent_warning", 20), mock.patch.object(
            handlers, "subnet_allow", {"keep"}
        ):
            flagged = handlers.check_for_low_ips(
                subnets, "vpc-1", "eu-west-1", metric_data
            )
        # Only the allow-listed subnet is evaluated and emits metrics.
        self.assertEqual([f[0] for f in flagged], ["keep"])
        self.assertEqual(
            {d["Value"] for e in metric_data for d in e["Dimensions"] if d["Name"] == "SubnetId"},
            {"keep"},
        )


class FlushMetricsTests(unittest.TestCase):
    def test_empty_metric_data_makes_no_api_call(self):
        with mock.patch.object(handlers.boto3, "client") as client:
            handlers.flush_metrics([])
            client.assert_not_called()

    def test_metrics_are_chunked(self):
        # 2300 entries with a chunk size of 1000 -> 3 PutMetricData calls.
        fake_cw = mock.Mock()
        data = [{"MetricName": "X", "Dimensions": [], "Value": 1, "Unit": "Count"}] * 2300
        with mock.patch.object(handlers.boto3, "client", return_value=fake_cw):
            handlers.flush_metrics(data)
        self.assertEqual(fake_cw.put_metric_data.call_count, 3)


if __name__ == "__main__":
    unittest.main()
