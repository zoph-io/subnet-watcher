.DEFAULT_GOAL ?= help
.PHONY: help

.PHONY: default
default: help;

help:
	@echo "Name: ${Product}-${Project}"
	@echo "Description: ${Description}"
	@echo "Credits: zoph.io - https://zoph.io"
	@echo ""
	@echo "Available commands:"
	@echo "	build - build artifacts ${Product} for ${Project}"
	@echo "	deploy - deploy ${Product} for ${Project} - also run 'build' command"
	@echo "	---"
	@echo "	delete - delete ${Product} for ${Project}"
	@echo "	clean - clean the build folder and artifacts"

###################### Parameters ######################
Product := subnet-watcher
Project := myproject
Environment := sandbox

AWSRegion := eu-west-1

# Alerting
PercentageRemainingWarning := 5
AlertsRecipient := john.doe@contoso.com

# Generated
Description := ${Product} - ${Project} - ${Environment}
#######################################################

build: clean
	sam build

deploy: build
	sam deploy \
		-t .aws-sam/build/template.yaml \
		--region ${AWSRegion} \
		--stack-name "${Project}-${Product}-${Environment}" \
		--capabilities CAPABILITY_IAM \
		--resolve-s3 \
		--force-upload \
		--parameter-overrides \
			pProjectName=${Project} \
			pProductName=${Product} \
			pDescription='${Description}' \
			pEnv=${Environment} \
			pAWSRegion=${AWSRegion} \
			pAlertsRecipient='${AlertsRecipient}' \
			pPercentageRemainingWarning=${PercentageRemainingWarning} \
		--no-fail-on-empty-changeset

delete:
	sam delete --stack-name "${Project}-${Product}-${Environment}"

clean:
	@rm -fr build/
	@rm -fr dist/
	@rm -fr htmlcov/
	@rm -fr site/
	@rm -fr .eggs/
	@rm -fr .tox/
	@rm -fr .aws-sam/
	@find . -name '*.egg-info' -exec rm -fr {} +
	@find . -name '.DS_Store' -exec rm -fr {} +
	@find . -name '*.egg' -exec rm -f {} +
	@find . -name '*.pyc' -exec rm -f {} +
	@find . -name '*.pyo' -exec rm -f {} +
	@find . -name '*~' -exec rm -f {} +
	@find . -name '__pycache__' -exec rm -fr {} +
