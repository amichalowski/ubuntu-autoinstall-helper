#!/bin/sh
if [ "$1" = "list-timezones" ]; then
    find -L /usr/share/zoneinfo -type f -not -name "*.tab" -not -name "*.list" -not -name "*.zi" | sed "s|^/usr/share/zoneinfo/||" | sort
    exit 0
fi
exit 0
