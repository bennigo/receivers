#!/bin/sh
# convert t00/T00 data in batches from cgps sites
# HG feb 2007 (based on older stuff)
# HG sep 2007: Adopt from brot to rek
# HG dec 2009: Use dummp instead of brot 
# HG mar 2010: adapt to 1hr1hz instead of 24hr15s. Use a-x for session index
######################

RBINDIR=/home/gpsops/bin/
LBINDIR=/home/gpsops/bin
TEQCPATH=/home/gpsops/bin
CONPATH=/home/gpsops/bin/

for LFILE in `ls $*`
do
   #First a small check to ensure we have some files to work with
   LCHR=`echo $LFILE | wc -c` 
   if [ $LCHR -lt 8 ]; then echo "$LFILE is not a valid filename, exiting..."; exit; fi
   echo "working on $LFILE..."
   LAPBAS=`basename $LFILE .tps`
   echo "LAPBAS: "$LAPBAS
   SSS=`echo $LAPBAS | cut -c1-4`
   echo "SSS: "$SSS
   sss=`echo $SSS | tr "[A-Z]" "[a-z]"`
   CONFIL=config-${sss}
   if [ -f ${CONPATH}${CONFIL} ] 
   then 
     echo "Found config file" > /dev/null
   else 
     echo "no config file ${CONPATH}${CONFIL} found, exiting..."; exit 
   fi
   # Find out the preferred name...
   #YEAR=`echo $LAPBAS | cut -c5-8`
   Y=`date +%y`
   YEAR="20"$Y
   echo "Year: "$YEAR
   MON=`echo $LAPBAS | cut -c5-6`
   echo "Month: "$MON
   DAY=`echo $LAPBAS | cut -c7-8`
   echo "Day: "$DAY
   H=`echo $LAPBAS | cut -c9`
   echo "Hour: "$H
   
   if [ $H = "a" ]; then HOUR=00; fi
   if [ $H = 'b' ]; then HOUR=01; fi 
   if [ $H = 'c' ]; then HOUR=02; fi
   if [ $H = 'd' ]; then HOUR=03; fi
   if [ $H = 'e' ]; then HOUR=04; fi
   if [ $H = 'f' ]; then HOUR=05; fi
   if [ $H = 'g' ]; then HOUR=06; fi
   if [ $H = 'h' ]; then HOUR=07; fi
   if [ $H = 'i' ]; then HOUR=08; fi
   if [ $H = 'j' ]; then HOUR=09; fi
   if [ $H = 'k' ]; then HOUR=10; fi
   if [ $H = 'l' ]; then HOUR=11; fi
   if [ $H = 'm' ]; then HOUR=12; fi
   if [ $H = 'n' ]; then HOUR=13; fi
   if [ $H = 'o' ]; then HOUR=14; fi
   if [ $H = 'p' ]; then HOUR=15; fi
   if [ $H = 'q' ]; then HOUR=16; fi
   if [ $H = 'r' ]; then HOUR=17; fi
   if [ $H = 's' ]; then HOUR=18; fi
   if [ $H = 't' ]; then HOUR=19; fi
   if [ $H = 'u' ]; then HOUR=20; fi
   if [ $H = 'v' ]; then HOUR=21; fi
   if [ $H = 'w' ]; then HOUR=22; fi
   if [ $H = 'x' ]; then HOUR=23; fi

   #echo $HOUR

   DDD=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $1}'`
   SESS=`${LBINDIR}/lapd2dddsess_met.pl $YEAR $MON $DAY $HOUR | awk '{print $2}'`
   YY=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $3}'`
   WEEK=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $4}'`
   RELBAS=${SSS}${DDD}${SESS}
   #RELBAS2=${SSS}${DDD}

   echo "DDD: "$DDD
   echo "SESS: "$SESS
   echo "YY: "$YY 
   echo "WEEK: "$WEEK

   # extract files from the
   # ${RBINDIR}runpkr00 -dfeim $LFILE     #obsjón f ef vandræði

   DATFIL=${RELBAS}.dat
   OBSFIL=${RELBAS}.${YY}O
   DCFIL=${RELBAS}.${YY}D.Z

   # ...and rename according to that
#   /bin/mv ${LAPBAS}.dat ${RELBAS}.dat
#   /bin/mv ${LAPBAS}.ion ${RELBAS}.ion
#   /bin/mv ${LAPBAS}.mes ${RELBAS}.mes
#   /bin/mv ${LAPBAS}.eph ${RELBAS}.eph

   #translate data from dat to rinex #and qc
   #${TEQCPATH}/teqc -week ${WEEK}  $LFILE > $OBSFIL
    ${TEQCPATH}/teqc $LFILE > $OBSFIL
   ${TEQCPATH}/teqc +err err.lst -config ${CONPATH}/${CONFIL} $OBSFIL >tmp2.$$
   mv tmp2.$$ $OBSFIL
   /home/gpsops/bin/thjap $OBSFIL

   # move and clean up
#   rm ${RELBAS}.dat ${RELBAS}.ion ${RELBAS}.mes ${RELBAS}.eph
   rsync -uva $DCFIL sil@frumgogn:/home/sil/gps_01/rinex/$YEAR/$SSS/
   rm $LFILE
   rm err.lst
done
rm * 
