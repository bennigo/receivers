#!/bin/sh
BINDIR=/home/gpsops/bin

YEAR=`$BINDIR/makemyday.pl 1 | awk '{print $1}'`
YY=`$BINDIR/makemyday.pl 1 | awk '{print $2}'`
DOY=`$BINDIR/makemyday.pl 1 | awk '{print $3}'`
STATION=REYK

cd /data/REYK

wget -S --passive-ftp ftp://ftp.lmi.is/GPS/REYK/REYK${DOY}0.${YY}D.Z
if [ -s REYK${DOY}0.${YY}D.Z ]
then
 rsync -uv --remove-sent-files -e ssh --timeout=30 REYK${DOY}0.${YY}D.Z sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$STATION/

else
 # try getting data from cddis:
 rm REYK${DOY}0.${YY}D.Z
 wget -S --passive-ftp ftp://igs.bkg.bund.de/IGS/obs/${YEAR}/${DOY}/reyk${DOY}0.${YY}d.Z -O REYK${DOY}0.${YY}D.Z 
 if [ -s REYK${DOY}0.${YY}D.Z ]
 then 
  echo "REYK file is 30s data from cddis"  > /tmp/reyktmp.tmp
  rsync -uv --remove-sent-files -e ssh --timeout=30 REYK${DOY}0.${YY}D.Z sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$STATION/
  /usr/bin/Mail -s reyk_data $superv < /tmp/reyktmp.tmp ; rm /tmp/reyktmp.tmp
 else
  rm REYK${DOY}0.${YY}D.Z
 fi
fi


