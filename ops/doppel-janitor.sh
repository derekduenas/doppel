#!/bin/sh
# A stale production unit must hold local tape until the policy-bound unit is installed.
echo 'Install the canonical policy-bound doppel-janitor.service before pruning tape.' >&2
exit 1
