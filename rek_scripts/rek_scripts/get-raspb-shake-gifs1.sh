#!/bin/bash

# skriftin sækir fyrri tromlurits gif-myndir dagsins frá raspberry shake mæli

STATION="svg"
DAY=`date '+%Y%m%d'`
SERVER=10.4.1.86
DATADIR="/home/gpsops/raspberry-shake/$STATION"


#sækjum fyrri myndir dagsins á Raspberry shake 
/usr/bin/scp -P 2251 myshake@$SERVER:/opt/data/gifs/*$DAY"00"*.gif $DATADIR
#myndir endurnefndar...
/bin/cp -rf  $DATADIR/*EHZ*$DAY"00"*.gif $DATADIR/morning-Z-channel.gif 
/bin/cp -rf  $DATADIR/*EHN*$DAY"00"*.gif $DATADIR/morning-N-channel.gif
/bin/cp -rf  $DATADIR/*EHE*$DAY"00"*.gif $DATADIR/morning-E-channel.gif
#...og sendar á brunn
scp $DATADIR/morning-?-channel.gif gpsops@sarpur.vedur.is:/srv/www/brunnur/gps/skridurogflod/svinafellsheidi/
# myndum eytt
rm $DATADIR/morning-?-channel.gif
