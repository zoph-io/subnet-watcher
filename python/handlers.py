import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
import logging
import os
import ipaddress

# Required configuration
percent_warning = int(os.environ["PERCENTAGE_REMAINING_WARNING"])
sns_topic_arn = os.environ["SNS_TOPIC_ARN"]
subject = os.environ["MESSAGE_SUBJECT"]

# Optional configuration
# - SCAN_ALL_REGIONS: when "true", iterate every enabled region. Default is to
#   scan only the deployment region (REGION_ID), which is far cheaper/faster.
# - SUBNET_IDS: comma-separated allow-list; when set, only these subnets are
#   monitored. Empty means "all subnets in scope".
# - LAMBDA_NOTIFICATIONS: when "true" (default), the function emails a detailed
#   per-subnet alert via SNS. Set "false" to rely solely on the CloudWatch alarm.
scan_all_regions = os.environ.get("SCAN_ALL_REGIONS", "false").lower() == "true"
subnet_allow = {s.strip() for s in os.environ.get("SUBNET_IDS", "").split(",") if s.strip()}
lambda_notifications = os.environ.get("LAMBDA_NOTIFICATIONS", "true").lower() == "true"

NAMESPACE = "VPCSubnetMetrics"

# PutMetricData accepts up to 1000 metrics per request; batch up to this many.
METRIC_CHUNK_SIZE = 1000

# Fail fast on unreachable/slow regions instead of hanging on the default 60s
# connect timeout, which matters when iterating many regions under the Lambda
# execution timeout.
BOTO_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"max_attempts": 3, "mode": "standard"},
)

# Logging configuration
root = logging.getLogger()
if root.handlers:
    for handler in root.handlers:
        root.removeHandler(handler)
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)


def _subnet_metric_data(subnet_id, vpc, available_ips, total_ips, percent_remaining):
    """Build the three per-subnet metric entries for PutMetricData.

    - `AvailableIpAddressCount` - Quantity of IP Addresses available
    - `TotalIpAddressCount` - Total usable IPs in subnet (CIDR size minus 5 reserved)
    - `AvailableIpAddressPercent` - Percentage of available to total IP Addresses
    """
    dimensions = [
        {"Name": "VPCId", "Value": vpc},
        {"Name": "SubnetId", "Value": subnet_id},
    ]
    return [
        {
            "MetricName": "AvailableIpAddressCount",
            "Dimensions": dimensions,
            "Value": available_ips,
            "Unit": "Count",
        },
        {
            "MetricName": "TotalIpAddressCount",
            "Dimensions": dimensions,
            "Value": total_ips,
            "Unit": "Count",
        },
        {
            "MetricName": "AvailableIpAddressPercent",
            "Dimensions": dimensions,
            "Value": percent_remaining,
            "Unit": "Percent",
        },
    ]


def _eni_metric_data(region, eni_count):
    """Build the region-wide detached-ENI metric entry."""
    return {
        "MetricName": "AvailableNetworkInterface",
        "Dimensions": [{"Name": "Region", "Value": region}],
        "Value": eni_count,
        "Unit": "Count",
    }


def flush_metrics(metric_data):
    """Publish accumulated metrics in batched PutMetricData calls.

    Batching across all subnets in a region (rather than one call per subnet)
    minimizes API calls and the chance of throttling on large accounts.
    """
    if not metric_data:
        return
    cloudwatch = boto3.client("cloudwatch", config=BOTO_CONFIG)
    for i in range(0, len(metric_data), METRIC_CHUNK_SIZE):
        batch = metric_data[i : i + METRIC_CHUNK_SIZE]
        try:
            cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=batch)
            logging.info("CW PutMetricData succeeded for %d metric(s)", len(batch))
        except (ClientError, BotoCoreError) as err:
            logging.error("Error while pushing custom metrics to CW: %s", err)


def check_for_low_ips(subnets, vpc, region, metric_data):
    """Evaluate subnets, append their metrics to `metric_data`, return low ones."""
    subnets_with_low_ips = []
    logging.info("Checking: %s in %s for %s", subnets, vpc, region)
    for subnet in subnets:
        # Optional allow-list: skip subnets that are not explicitly monitored.
        if subnet_allow and subnet.id not in subnet_allow:
            continue

        available_ips = subnet.available_ip_address_count
        # CIDR size minus the 5 IPs AWS reserves in every subnet.
        total_ips = ipaddress.ip_network(subnet.cidr_block).num_addresses - 5

        if total_ips <= 0:
            logging.warning(
                "Skipping %s in %s (%s): non-positive usable IP count (cidr=%s)",
                subnet.id,
                vpc,
                region,
                subnet.cidr_block,
            )
            continue

        percent_remaining = round(available_ips / total_ips * 100, 2)

        metric_data.extend(
            _subnet_metric_data(
                subnet.id, vpc, available_ips, total_ips, percent_remaining
            )
        )

        if percent_remaining <= percent_warning:
            logging.info(
                "Low Ips available (%s) in: %s in %s for %s with %s%% remaining",
                available_ips,
                subnet.id,
                vpc,
                region,
                percent_remaining,
            )
            subnets_with_low_ips.append([subnet.id, vpc, region, percent_remaining])
        else:
            logging.info(
                "Its fine for %s in: %s for %s with %s%% (%s) remaining",
                subnet.id,
                vpc,
                region,
                percent_remaining,
                available_ips,
            )

    return subnets_with_low_ips


def count_available_enis(region):
    ec2 = boto3.client("ec2", region_name=region, config=BOTO_CONFIG)

    # Paginate so accounts with many ENIs are not silently truncated.
    paginator = ec2.get_paginator("describe_network_interfaces")
    count = 0
    for page in paginator.paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    ):
        count += len(page["NetworkInterfaces"])

    return count


def list_vpc_ids(region):
    ec2 = boto3.client("ec2", region_name=region, config=BOTO_CONFIG)

    paginator = ec2.get_paginator("describe_vpcs")
    vpc_ids = []
    for page in paginator.paginate():
        for vpc in page["Vpcs"]:
            vpc_ids.append(vpc["VpcId"])

    return vpc_ids


def send_notification(subnets_flagged, eni_counts):
    message_txt = ""
    for subnet in subnets_flagged:
        message_txt += "Subnet: {} in {} for {} has {}% remaining IP addresses available!\n\n".format(
            subnet[0], subnet[1], subnet[2], subnet[3]
        )
    message_txt += "\nAvailable (Detached) Elastic Network Interfaces (ENI) per region:\n"
    for region, count in eni_counts.items():
        message_txt += "  {}: {}\n".format(region, count)

    notify = boto3.client("sns", config=BOTO_CONFIG)
    logging.info("Sending Alert: %s", message_txt)

    try:
        notify.publish(TargetArn=sns_topic_arn, Subject=subject, Message=message_txt)
    except (ClientError, BotoCoreError) as error:
        logging.error("Error while trying to publish sns message: %s", error)


def process_region(region, vpc_id=None):
    """Collect subnet metrics for a region. Returns (flagged_subnets, eni_count)."""
    metric_data = []

    eni_count = count_available_enis(region)
    metric_data.append(_eni_metric_data(region, eni_count))

    flagged = []
    ec2_resource = boto3.resource("ec2", region_name=region, config=BOTO_CONFIG)

    vpc_ids = [vpc_id] if vpc_id else list_vpc_ids(region)
    for current_vpc_id in vpc_ids:
        vpc_object = ec2_resource.Vpc(current_vpc_id)
        low_ips = check_for_low_ips(
            list(vpc_object.subnets.all()), vpc_object.vpc_id, region, metric_data
        )
        if low_ips:
            flagged.extend(low_ips)
        else:
            logging.info("No low ip detected in %s for %s", current_vpc_id, region)

    # One batched publish per region (covers the ENI metric + every subnet).
    flush_metrics(metric_data)

    return flagged, eni_count


def main(event, context):
    subnets_flagged = []
    eni_counts = {}

    if scan_all_regions:
        region_client = boto3.client("ec2", config=BOTO_CONFIG)
        regions = region_client.describe_regions()["Regions"]

        for region in regions:
            region_name = region["RegionName"]
            logging.info("Checking: %s", region_name)
            try:
                flagged, eni_count = process_region(region_name)
                subnets_flagged.extend(flagged)
                eni_counts[region_name] = eni_count
            except (ClientError, BotoCoreError) as err:
                # A disabled/opt-in region (AuthFailure) or an unreachable
                # endpoint (connect timeout) must not kill the whole run.
                logging.warning("Skipping region %s: %s", region_name, err)
                continue
    else:
        region_env = os.environ.get("REGION_ID", "")
        if region_env == "":
            session = boto3.session.Session()
            region_id = session.region_name
            logging.info("REGION_ID not set, defaulting to region %s", region_id)
        else:
            region_id = region_env

        vpc_env = os.environ.get("VPC_ID", "")
        vpc_id = vpc_env if vpc_env != "" else None
        flagged, eni_count = process_region(region_id, vpc_id=vpc_id)
        subnets_flagged.extend(flagged)
        eni_counts[region_id] = eni_count

    # Notifications
    if subnets_flagged and lambda_notifications:
        logging.info("Sending SNS Notification to alert recipients")
        send_notification(subnets_flagged, eni_counts)
    elif subnets_flagged:
        logging.info(
            "Low subnet(s) detected but LAMBDA_NOTIFICATIONS is disabled; "
            "relying on the CloudWatch alarm"
        )
    else:
        logging.info("No flagged subnet, no notification")


if __name__ == "__main__":
    main(0, 0)
