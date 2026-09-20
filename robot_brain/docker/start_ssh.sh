#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${PROJECT_ROOT:-/mlsteam/workspace}"
mkdir -p /root/.ssh /run/sshd
chmod 700 /root/.ssh
if [[ -f "${PROJECT_ROOT}/.ssh/authorized_keys" ]]; then
  cp "${PROJECT_ROOT}/.ssh/authorized_keys" /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi
/usr/sbin/sshd
pgrep -af sshd
