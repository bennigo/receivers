#!/bin/bash
# Get AKUR data from lmi ftp server. 
# adapted for gnsmart server
# BGO Oct 2016
##################################
if [ $# -lt 1 ]; then
        echo "Usage: `basename $0` OLDNESS " 1>&2
        echo "    OLDNESS is number of days since today (1 being yesterday)" 1>&2
        echo "    Setting oldness to 1 (default)"
        OLDNESS=1
else
        OLDNESS=$1
fi

STATION=HEID

/home/gpsops/bin/sync_lmi-15s_data.sh ${OLDNESS} ${STATION}
