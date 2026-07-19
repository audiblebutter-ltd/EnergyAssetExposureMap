#!/usr/bin/env bash
# Deploys the Energy Asset Exposure Map stack.
# Usage: ./deploy.sh <region> <profile> [environment]
# e.g.   ./deploy.sh eu-west-2 default dev
#
# NOTE: written to match the sibling Global Shock pipeline's deploy.sh
# pattern (CloudFormation with placeholder code, then update-function-code
# per Lambda). Has not been run against AWS yet - review before running.
set -euo pipefail

REGION="${1:?Usage: ./deploy.sh <region> <profile> [environment]}"
PROFILE="${2:?Usage: ./deploy.sh <region> <profile> [environment]}"
ENVIRONMENT="${3:-dev}"
PROJECT_NAME="energy-asset-exposure-map"
STACK_NAME="${PROJECT_NAME}-${ENVIRONMENT}"

AWS="aws --region ${REGION} --profile ${PROFILE}"

echo "==> Deploying CloudFormation stack ${STACK_NAME}"
${AWS} cloudformation deploy \
  --template-file infra/cloudformation.yaml \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName="${PROJECT_NAME}" Environment="${ENVIRONMENT}"

deploy_lambda() {
  local FUNCTION_SUFFIX="$1"   # e.g. ingest-assets
  local LAMBDA_DIR="$2"        # e.g. lambdas/ingest_assets
  local FUNCTION_NAME="${PROJECT_NAME}-${ENVIRONMENT}-${FUNCTION_SUFFIX}"
  local STAGE_DIR
  STAGE_DIR="$(mktemp -d)"

  echo "==> Packaging ${FUNCTION_NAME}"
  cp -r "${LAMBDA_DIR}/." "${STAGE_DIR}/"
  cp -r shared "${STAGE_DIR}/shared"

  # ingest_hazards bundles the hand-curated storm catalogue alongside its handler
  if [ "${LAMBDA_DIR}" = "lambdas/ingest_hazards" ]; then
    mkdir -p "${STAGE_DIR}/data"
    cp data/storm_events.json "${STAGE_DIR}/data/storm_events.json"
  fi

  # Portable stand-in for `zip -r` - not every dev machine has the zip CLI
  # (this one doesn't), but python3 + zipfile is always available. On
  # git-bash/MSYS, python3 resolves to native Windows Python, which can't
  # read MSYS-style /tmp/... paths, so translate with cygpath first.
  local STAGE_DIR_WIN
  STAGE_DIR_WIN="$(cygpath -w "${STAGE_DIR}" 2>/dev/null || echo "${STAGE_DIR}")"
  python3 -c "import shutil; shutil.make_archive(r'${STAGE_DIR_WIN}', 'zip', r'${STAGE_DIR_WIN}')"

  echo "==> Updating code for ${FUNCTION_NAME}"
  ${AWS} lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --zip-file "fileb://${STAGE_DIR_WIN}.zip" \
    --output text > /dev/null

  rm -rf "${STAGE_DIR}" "${STAGE_DIR}.zip"
}

deploy_lambda "ingest-assets" "lambdas/ingest_assets"
deploy_lambda "ingest-hazards" "lambdas/ingest_hazards"
deploy_lambda "join" "lambdas/join"

echo "==> Done. processed/latest.json will appear at:"
${AWS} cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query "Stacks[0].Outputs[?OutputKey=='ProcessedLatestUrl'].OutputValue" \
  --output text

read -p "==> Run ingestion now to populate the first snapshot? (y/N) " -n 1 -r
echo
if [[ "${REPLY}" =~ ^[Yy]$ ]]; then
  ${AWS} lambda invoke --function-name "${PROJECT_NAME}-${ENVIRONMENT}-ingest-assets" /tmp/ingest_assets_response.json
  ${AWS} lambda invoke --function-name "${PROJECT_NAME}-${ENVIRONMENT}-ingest-hazards" /tmp/ingest_hazards_response.json
  echo "Ingestion invoked - the join Lambda will fire automatically off the S3 raw-object-created event."
fi
