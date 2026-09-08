#!/usr/bin/env bash
# Check the deployment configuration before any AWS credential exists.
#
# These come from GitHub Environment *variables*, not secrets: a certificate
# ARN, a hostname and a sender address are deployment configuration, and
# putting them in secrets would only hide them from the run log that needs to
# record what was deployed.
#
# The failure this prevents is quiet. A deploy with these unset synthesises a
# stack with no listener, no application URL and an empty sender: CloudFormation
# succeeds, the workflow reports success, and the environment is unreachable and
# cannot authenticate anyone. Failing here, before the environment approval is
# spent and before a role is assumed, is the cheap moment.
set -euo pipefail

STACK="${STACK:-}"
CERTIFICATE_ARN="${CERTIFICATE_ARN:-}"
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-}"
SIGN_IN_SENDER="${SIGN_IN_SENDER:-}"
CUSTOMER_ID="${CUSTOMER_ID:-}"
CUSTOMER_ENVIRONMENT_ID="${CUSTOMER_ENVIRONMENT_ID:-}"
DEPLOYMENT_ID="${DEPLOYMENT_ID:-}"
DATA_CLASS="${DATA_CLASS:-}"

problems=()

require() {
  local name="$1" value="$2"
  if [ -z "$value" ]; then
    problems+=("$name is not set; add it as a GitHub Environment variable on 'nonproduction'")
    return 1
  fi
}

# Every stack needs these, because app.py builds all five stacks on every
# synthesis: CorridorApplication's guards run even when deploying only the
# network.
for key in CUSTOMER_ID CUSTOMER_ENVIRONMENT_ID DEPLOYMENT_ID; do
  value="${!key}"
  require "CORRIDOR_$key" "$value" &&
    if ! printf '%s' "$value" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$'; then
      problems+=("CORRIDOR_$key must be a stable bounded identifier")
    fi
done
if [ "$DATA_CLASS" != "synthetic" ]; then
  problems+=("CORRIDOR_DEPLOYMENT_DATA_CLASS must explicitly be synthetic for this #489 environment")
fi

require CORRIDOR_CERTIFICATE_ARN "$CERTIFICATE_ARN" &&
  if ! printf '%s' "$CERTIFICATE_ARN" |
      grep -Eq '^arn:aws:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-fA-F-]+$'; then
    problems+=("CORRIDOR_CERTIFICATE_ARN is not an ACM certificate ARN: '$CERTIFICATE_ARN'")
  fi

require CORRIDOR_PUBLIC_HOSTNAME "$PUBLIC_HOSTNAME" &&
  {
    case "$PUBLIC_HOSTNAME" in
      *://*|*/*|*' '*)
        problems+=("CORRIDOR_PUBLIC_HOSTNAME must be a bare DNS name with no scheme or path: '$PUBLIC_HOSTNAME'")
        ;;
      *)
        if ! printf '%s' "$PUBLIC_HOSTNAME" |
            grep -Eq '^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$'; then
          problems+=("CORRIDOR_PUBLIC_HOSTNAME is not a DNS hostname: '$PUBLIC_HOSTNAME'")
        fi
        ;;
    esac
  }

require CORRIDOR_SIGN_IN_SENDER "$SIGN_IN_SENDER" &&
  if ! printf '%s' "$SIGN_IN_SENDER" |
      grep -Eq '^[^[:space:]@]+@[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$'; then
    problems+=("CORRIDOR_SIGN_IN_SENDER is not an email address: '$SIGN_IN_SENDER'")
  fi

# The sender has to be verifiable under a domain related to the site, or SES
# will reject it and no link is ever delivered. A warning rather than a failure:
# a separate verified domain is a legitimate choice.
if [ -n "$PUBLIC_HOSTNAME" ] && [ -n "$SIGN_IN_SENDER" ]; then
  sender_domain="${SIGN_IN_SENDER##*@}"
  case "$PUBLIC_HOSTNAME" in
    *"$sender_domain") ;;
    *)
      echo "::warning::sender domain '$sender_domain' is not part of" \
           "'$PUBLIC_HOSTNAME'; confirm it is a verified SES identity"
      ;;
  esac
fi

if [ ${#problems[@]} -gt 0 ]; then
  for problem in "${problems[@]}"; do
    echo "::error::$problem"
  done
  exit 1
fi

echo "deployment configuration for ${STACK:-all stacks}:"
echo "  certificate : ${CERTIFICATE_ARN%%/*}/..."
echo "  hostname    : $PUBLIC_HOSTNAME"
echo "  sender      : $SIGN_IN_SENDER"
echo "  customer    : $CUSTOMER_ID"
echo "  environment : $CUSTOMER_ENVIRONMENT_ID"
echo "  deployment  : $DEPLOYMENT_ID"
echo "  data class  : $DATA_CLASS"
