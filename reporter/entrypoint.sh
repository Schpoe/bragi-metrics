#!/bin/bash
set -e

printenv | grep -E '^(POSTGRES|REPORTS)' >> /etc/environment

touch /var/log/reporter.log
cron

echo "Reporter service running. Cron scheduled for monthly/quarterly reports."
tail -f /var/log/reporter.log
