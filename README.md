# 🔍 Subnet Watcher

## 🧠 Rationale - Problem to solve

AWS does not provide any CloudWatch (CW) metrics to monitor available IPs in VPC subnets. It can be difficult to avoid shortages and get alerted when you are approaching the limit.

## 📝 Description

Subnet-Watcher monitors the remaining free IP addresses in AWS VPC subnets (both public and private) using some CloudWatch custom metrics. It also sets up alerts to provide complete visibility on your VPC CIDR IP space.

- `AvailableIpAddressCount` - Number of IP Addresses available
- `TotalIpAddressCount` - Quantity of Total IP Addresses in subnet (based on CIDR size minus the [5 AWS reserved Ips](https://docs.aws.amazon.com/vpc/latest/userguide/configure-subnets.html))
- `AvailableIpAddressPercent` - Percentage of available IP Addresses
- `AvailableNetworkInterface` - Number of Elastic Network Interfaces Available (ENI) in VPC (with `status` = `available`)

### Sample

![CW Metrics](https://user-images.githubusercontent.com/20846187/214828070-edde41d9-e903-418d-8665-1c1f71856b26.png)

AWS recently released [VPC IPAM](https://docs.aws.amazon.com/vpc/latest/ipam/what-it-is-ipam.html), however, I have not been able to find any benefits from it and it seems to be a costly solution to this problem.

## 🎛 Parameters

Change it in the `Makefile`

|         Parameter          |              Description               | Required | Default Value  |
| :------------------------: | :------------------------------------: | :------: | :------------: |
|          Product           |          Name of the Product           |  `yes`   | subnet-watcher |
|          Project           |          Name of your Project          |  `yes`   |                |
|        Environment         |        Name of your environment        |  `yes`   |                |
|         AWSRegion          | Used AWS Region (target of deployment) |  `yes`   |  `eu-west-1`   |
|      AlertsRecipient       |   Recipient of SNS Message (Alerts)    |  `yes`   |                |
| PercentageRemainingWarning |  Percentage Remaining IP for alerting  |  `yes`   |      `20`      |

_Optional:_ You can modify the CloudFormation template to specify the `VPC_ID` (empty by default) as an environment variable, which will cause the check to be performed on the specified VPC.

## 🚀 Deployment

    $ make deploy

## 🎖️ Credits

I was inspired by the following projects and decided to create my own version that met my specific needs and included some additional features.

- https://github.com/buzzsurfr/VpcSubnetIpMonitor
- https://github.com/mstockwell/Check-VPC-IP-Address-Space
