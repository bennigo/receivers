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
   if [ $LCHR -lt 20 ]; then echo "$LFILE is not a valid filename, exiting..."; exit; fi
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
   #Y=`date +%y`
   #YEAR="20"$Y
   #echo "Year: "$YEAR
   #MON=`echo $LAPBAS | cut -c5-8`
   #echo "Month: "$MON
   #DAY=`echo $LAPBAS | cut -c9-10`
   #echo "Day: "$DAY
   #H=`echo $LAPBAS | cut -c9`
   echo "Hour: "$H
     YEAR=`echo $LAPBAS | cut -c5-8`
     MON=`echo $LAPBAS | cut -c9-10`
     DAY=`echo $LAPBAS | cut -c11-12`
     HOUR=`echo $LAPBAS | cut -c13-14`
   

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
   #/home/gpsops/bin/thjap $OBSFIL

   # move and clean up
#   rm ${RELBAS}.dat ${RELBAS}.ion ${RELBAS}.mes ${RELBAS}.eph
   rsync -uv --remove-sent-files --timeout=30 $DCFIL sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$SSS/
   rm $LFILE
   rm err.lst
done 
