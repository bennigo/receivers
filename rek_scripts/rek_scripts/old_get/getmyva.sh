#!/bin/sh
# Get MYVA 24hr 15 sec data from yesterday from lmi ftp server.
# HG jan 2007
# HG dec 2009: Use dump instead of brot
##################################

function download(){

echo "This is OLDNESS" $OLDNESS

YEAR=`/home/gpsops/bin/makemyday.pl $OLDNESS | awk '{print $1}'`
YR=`/home/gpsops/bin/makemyday.pl $OLDNESS | awk '{print $2}'`
DOY=`/home/gpsops/bin/makemyday.pl $OLDNESS | awk '{print $3}'`
STATION=MYVA
station=myva

cd /usr/sil/gps/tmp/
file="${station}${DOY}0.${YR}d.Z"
FILE="${STATION}${DOY}0.${YR}D.Z"
wget ftp://ftp.lrz.de/transfer/bekgps/MYVA-TRANSFER/${file} #2>/tmp/getmyva.tmp
echo "wget ftp://ftp.lrz.de/transfer/bekgps/MYVA-TRANSFER/${file} 2>/tmp/getmyva.tmp"

#wget -S --passive-ftp ftp://ftp.lmi.is/GPS/MYVA/MYVA${DOY}0.${YR}D.Z  2>/tmp/getmyva.tmp

mv ${file} ${FILE}


FNUM=`ls ${FILE} | wc -l`
echo $FNUM
if [ $FNUM -gt 0 ]
then
  # move to rinex directory:
  rsync -uv --remove-sent-files -e ssh --timeout=30 ${FILE} sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$STATION/
else
  echo "Problems getting 24 hr file MYVA${DOY}0.${YR}D.Z from ftp.lrz.de ftp server. Will look for 24 hour files..."
  /home/gpsops/bin/getmyva_1h.sh ${YR} ${DOY}
fi
}


if [ $1='']
then
    echo "No parameter given"
    OLDNESS=1
    download
else
   echo "Parameter $1 given"
   for((c=1; c<=$1; c++))
     do
       echo "This is the current i:" $c
       OLDNESS=$c
       download
     done

fi


