#!/bin/sh
# Get HEID data from lmi ftp server. 
# adapted for gnsmart server
# HG jul 2010
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
cd /data/$STATION
DOY=`/home/gpsops/bin/makemyday.pl $OLDNESS | awk '{print $3}'`
YR=`/home/gpsops/bin/makemyday.pl $OLDNESS | awk '{print $2}'`
YEAR=`/home/gpsops/bin/makemyday.pl $OLDNESS | awk '{print $1}'`
WEEK=`/home/gpsops/bin/makemyday.pl $OLDNESS | awk '{print $4}'`

echo " Checking for a 24 hour $STATION file on lmi ftp server"
wget -S --passive-ftp ftp://213.167.147.213/gnsmart_data/$YEAR/24h_30s_data/$WEEK/$DOY/${STATION}${DOY}a.${YR}e
if [ -s ${STATION}${DOY}a.${YR}e ] 
then 
  echo "Found file ${STATION}${DOY}a.${YR}e on lmi ftp server, renaming and moving file to rinex directoy on brot...."
  mv  ${STATION}${DOY}a.${YR}e  ${STATION}${DOY}0.${YR}D.Z
  rsync -uv --remove-sent-files -e ssh --timeout=30  ${STATION}${DOY}0.${YR}D.Z sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$STATION/
else 
  echo " No 24hr file found on lmi ftp server, exiting..."
  exit
fi
