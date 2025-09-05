#!/bin/bash

# skriftin sækir seinni tromlurits gif-myndir dagsins frá raspberry shake mæli

STATION="svg"
DAY=`date '+%Y%m%d'`
SERVER=10.4.1.86
DATADIR="/home/gpsops/raspberry-shake/$STATION"


#sækjum seinni myndir dagsins á Raspberry shake 
/usr/bin/scp -P 2251 myshake@$SERVER:/opt/data/gifs/*$DAY"12"*.gif $DATADIR
#myndir endurnefndar...
/bin/cp -rf  $DATADIR/*EHZ*$DAY"12"*.gif $DATADIR/afternoon-Z-channel.gif 
/bin/cp -rf  $DATADIR/*EHN*$DAY"12"*.gif $DATADIR/afternoon-N-channel.gif
/bin/cp -rf  $DATADIR/*EHE*$DAY"12"*.gif $DATADIR/afternoon-E-channel.gif
#...og sendar á brunn
scp $DATADIR/afternoon-?-channel.gif gpsops@sarpur.vedur.is:/srv/www/brunnur/gps/skridurogflod/svinafellsheidi/
# myndum eytt
rm $DATADIR/afternoon-?-channel.gif
