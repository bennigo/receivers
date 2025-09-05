#!/bin/bash

# skriftin sækir seinni tromlurits gif-myndir gærdagsins frá raspberry shake mæli

STATION="svg"
YESTERDAY=`date -d "yesterday" '+%Y%m%d'`
SERVER=10.4.1.86
DATADIR="/home/gpsops/raspberry-shake/$STATION"


#sækjum seinni myndir gærdagsins á Raspberry shake 
/usr/bin/scp -P 2251 myshake@$SERVER:/opt/data/gifs/*$YESTERDAY"12"*.gif $DATADIR
#myndir endurnefndar...
/bin/cp -rf  $DATADIR/*EHZ*$YESTERDAY"12"*.gif $DATADIR/Yesterday-afternoon-Z-channel.gif 
/bin/cp -rf  $DATADIR/*EHN*$YESTERDAY"12"*.gif $DATADIR/Yesterday-afternoon-N-channel.gif
/bin/cp -rf  $DATADIR/*EHE*$YESTERDAY"12"*.gif $DATADIR/Yesterday-afternoon-E-channel.gif
#...og sendar á brunn
scp $DATADIR/Yesterday-afternoon-?-channel.gif gpsops@sarpur.vedur.is:/srv/www/brunnur/gps/skridurogflod/svinafellsheidi/
# myndum eytt
rm $DATADIR/Yesterday-afternoon-?-channel.gif
