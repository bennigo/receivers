#!/bin/sh
BINDIR=/home/gpsops/bin

YEAR=`$BINDIR/makemyday.pl 1 | awk '{print $1}'`
YY=`$BINDIR/makemyday.pl 1 | awk '{print $2}'`
DOY=`$BINDIR/makemyday.pl 1 | awk '{print $3}'`
STATION=HOFN

cd /data/HOFN

wget -S --passive-ftp ftp://ftp.lmi.is/GPS/HOFN/HOFN${DOY}0.${YY}D.Z
if [ -s HOFN${DOY}0.${YY}D.Z ]
then
 rsync -uv --remove-sent-files -e ssh --timeout=30 HOFN${DOY}0.${YY}D.Z sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$STATION/

else
 # try getting data from cddis:
 rm HOFN${DOY}0.${YY}D.Z
 wget -S --passive-ftp ftp://igs.bkg.bund.de/IGS/obs/${YEAR}/${DOY}/hofn${DOY}0.${YY}d.Z -O HOFN${DOY}0.${YY}D.Z 
 if [ -s HOFN${DOY}0.${YY}D.Z ]
 then 
  echo "HOFN file is 30s data from cddis"  > /tmp/hofntmp.tmp
  rsync -uv --remove-sent-files -e ssh --timeout=30 HOFN${DOY}0.${YY}D.Z sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$STATION/
  /usr/bin/Mail -s hofn_data $superv < /tmp/hofntmp.tmp ; rm /tmp/hofntmp.tmp
 else
  rm HOFN${DOY}0.${YY}D.Z
 fi
fi


