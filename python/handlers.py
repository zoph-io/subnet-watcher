from __future__ import print_function
import boto3
from botocore.exceptions import ClientError
import logging
import os
import ipaddress

# Set variables
percent_warning = int(os.environ["PERCENTAGE_REMAINING_WARNING"])
sns_topic_arn = os.environ["SNS_TOPIC_ARN"]
subject = os.environ["MESSAGE_SUBJECT"]

# Logging configuration
root = logging.getLogger()
if root.handlers:
    for handler in root.handlers:
        root.removeHandler(handler)
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)


def check_for_low_ips(subnets, vpc, region, count_enis):
    subnets_with_low_ips = []
    logging.info("Checking: %s in %s for %s", subnets, vpc, region)
    for subnet in subnets:
        available_ips = subnet.available_ip_address_count
        total_ips = (ipaddress.ip_network(subnet.cidr_block).num_addresses) - 5
        percent_remaining = round(available_ips / total_ips, 2) * 100

        # Put custom metrics in CloudWatch Metrics
        put_cw_metrics(
            subnet.id, vpc, available_ips, total_ips, percent_remaining, count_enis
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

    ec2 = boto3.client("ec2", region_name=region)

    response = ec2.describe_network_interfaces(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )

    # Get the list of ENIs from the response
    enis = response["NetworkInterfaces"]
    enis_list = []

    for eni in enis:
        enis_list.append(eni["NetworkInterfaceId"])

    return len(enis_list)


def send_notification(subnets_flagged, count_enis):
    message_txt = ""
    for subnet in subnets_flagged:
        message_txt += "Subnet: {} in {} for {} has {}% remaining IP addresses available!\n\n".format(
            subnet[0], subnet[1], subnet[2], subnet[3]
        )
    message_txt += "\nAvailable (Detached) Elastic Network Interface (ENI): {}".format(
        str(count_enis)
    )
    notify = boto3.client("sns")
    logging.info("Sending Alert: %s", message_txt)

    try:
        notify.publish(TargetArn=sns_topic_arn, Subject=subject, Message=(message_txt))
    except ClientError as error:
        logging.error("Error while trying to publish sns message: %s", error)


def put_cw_metrics(
    subnet, vpc, available_ips, total_ips, percent_remaining, count_enis
):

    """
    - `AvailableIpAddressCount` - Quantity of IP Addresses available
    - `TotalIpAddressCount` - Quantity of Total IP Addresses in subnet (based on CIDR size minus [5 AWS reserved Ips](https://docs.aws.amazon.com/vpc/latest/userguide/configure-subnets.html))
    - `AvailableIpAddressPercent` - Percentage of Available to Total IP Addresses
    - `AvailableNetworkInterface` - Number of Elastic Network Interfaces Available (ENI) (not currently attached)
    """
    cloudwatch = boto3.client("cloudwatch")

    # AvailableIpAddressCount
    try:
        cloudwatch.put_metric_data(
            Namespace="VPCSubnetMetrics",
            MetricData=[
                {
                    "MetricName": "AvailableIpAddressCount",
                    "Dimensions": [
                        {"Name": "VPCId", "Value": vpc},
                        {"Name": "SubnetId", "Value": subnet},
                    ],
                    "Value": available_ips,
                    "Unit": "Count",
                },
            ],
        )
        logging.info("CW PutMetricData Succeeded for: AvailableIpAddressCount")
    except ClientError as err:
        logging.error(
            "[AvailableIpAddressCount] Error while pushing custom metrics to CW: %s",
            err,
        )

    # TotalIpAddressCount
    try:
        cloudwatch.put_metric_data(
            Namespace="VPCSubnetMetrics",
            MetricData=[
                {
                    "MetricName": "TotalIpAddressCount",
                    "Dimensions": [
                        {"Name": "VPCId", "Value": vpc},
                        {"Name": "SubnetId", "Value": subnet},
                    ],
                    "Value": total_ips,
                    "Unit": "Count",
                },
            ],
        )
        logging.info("CW PutMetricData Succeeded for: TotalIpAddressCount")
    except ClientError as err:
        logging.error(
            "[TotalIpAddressCount] Error while pushing custom metrics to CW: %s", err
        )

    # AvailableIpAddressPercent
    try:
        cloudwatch.put_metric_data(
            Namespace="VPCSubnetMetrics",
            MetricData=[
                {
                    "MetricName": "AvailableIpAddressPercent",
                    "Dimensions": [
                        {"Name": "VPCId", "Value": vpc},
                        {"Name": "SubnetId", "Value": subnet},
                    ],
                    "Value": percent_remaining,
                    "Unit": "Count",
                },
            ],
        )
        logging.info("CW PutMetricData Succeeded for: AvailableIpAddressPercent")
    except ClientError as err:
        logging.error(
            "[AvailableIpAddressPercent] Error while pushing custom metrics to CW: %s",
            err,
        )

    # AvailableNetworkInterface
    try:
        # count_enis is a list of dict if multiple regions, otherise its a simple int.
        for count_eni in count_enis:
            enis = count_eni["count"]
        cloudwatch.put_metric_data(
            Namespace="VPCSubnetMetrics",
            MetricData=[
                {
                    "MetricName": "AvailableNetworkInterface",
                    "Dimensions": [
                        {"Name": "VPCId", "Value": vpc},
                    ],
                    "Value": enis,
                    "Unit": "Count",
                },
            ],
        )
        logging.info("CW PutMetricData Succeeded for: AvailableNetworkInterface")
    except ClientError as err:
        logging.error(
            "[AvailableNetworkInterface] Error while pushing custom metrics to CW: %s",
            err,
        )


def main(event, context):
    subnets_flagged = []
    count_enis = []

    # If VPC_ID AND REGION_ID are NOT set, iterate all AWS regions
    if ("REGION_ID" not in os.environ or os.environ["REGION_ID"] == "") and (
        "VPC_ID" not in os.environ or os.environ["VPC_ID"] == ""
    ):
        region_client = boto3.client("ec2")

        # Getting list of all AWS Regions
        regions = region_client.describe_regions()

        for region in regions["Regions"]:
            logging.info("Checking: %s", region["RegionName"])

            vpc_client = boto3.client("ec2", region_name=region["RegionName"])

            # Get ENI count per region
            enis_count = count_available_enis(region["RegionName"])
            count_enis.append(
                {
                    "region": region["RegionName"],
                    "count": enis_count,
                }
            )

            # Get list of all VPCs in a single AWS Region
            vpcs = vpc_client.describe_vpcs()

            for vpc in vpcs["Vpcs"]:
                vpc_resource = boto3.resource("ec2", region_name=region["RegionName"])

                vpc_object = vpc_resource.Vpc(vpc["VpcId"])

                # For each VPC in a single AWS Region, check for low ips
                low_ips = check_for_low_ips(
                    list(vpc_object.subnets.all()),
                    vpc_object.vpc_id,
                    region["RegionName"],
                    count_enis,
                )

                if low_ips:
                    subnets_flagged.extend(low_ips)
                else:
                    logging.info(
                        "No low ip detected in %s for %s",
                        vpc_object.vpc_id,
                        region["RegionName"],
                    )
    else:
        # Checking single AWS Region
        if ("REGION_ID" not in os.environ or os.environ["REGION_ID"] == ""):
            session = boto3.session.Session()
            region_id = session.region_name
            logging.info("os.environ[\"REGION_ID\"] not set, defaulting to region %s", region_id)
        else: 
            region_id = os.environ["REGION_ID"]

        # Get ENI count on this specific region
        enis_count = count_available_enis(region_id)
        count_enis.append(
            {
                "region": region_id,
                "count": enis_count,
            }
        )

        vpc_client = boto3.client("ec2", region_name=region_id)

        if "VPC_ID" not in os.environ or os.environ["VPC_ID"] == "":
            # Get list of all VPCs in a single AWS Region
            vpcs = vpc_client.describe_vpcs()

            for vpc in vpcs["Vpcs"]:
                vpc_resource = boto3.resource("ec2", region_name=region_id)

                vpc_object = vpc_resource.Vpc(vpc["VpcId"])

                # For each VPC in a single AWS Region, check for low ips
                low_ips = check_for_low_ips(
                    list(vpc_object.subnets.all()),
                    vpc_object.vpc_id,
                    region_id,
                    count_enis,
                )

                if low_ips:
                    subnets_flagged.extend(low_ips)
                else:
                    logging.info(
                        "No low ip detected in %s for %s",
                        vpc_object.vpc_id,
                        region_id,
                    )
        else:
            vpc_resource = boto3.resource("ec2", region_name=region_id)
            vpc_object = vpc_resource.Vpc(os.environ["VPC_ID"])

            subnets_flagged = check_for_low_ips(
                    list(vpc_object.subnets.all()),
                    vpc_object.vpc_id,
                    region_id,
                    count_enis,
            )

    # Notifications
    if subnets_flagged:
        logging.info("Sending SNS Notification to alert recipients")
        send_notification(subnets_flagged, count_enis)
    else:
        logging.info("No flagged subnet, no notification")


if __name__ == "__main__":
    main(0, 0)
