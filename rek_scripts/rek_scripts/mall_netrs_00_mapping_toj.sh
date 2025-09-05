#!/bin/sh
# convert t00/T00 data in batches from cgps sites
# HG feb 2007 (based on older stuff)
# HG sep 2007: Adopt from brot to rek
# HG dec 2009: Use dummp instead of brot 
# FS/ToJ jul 2011: Added DAT/Topcon file processing and made some other changes
######################

BINDIR=/home/gpsops/bin/
LBINDIR=/home/gpsops/bin
TEQCPATH=/home/gpsops/bin/teqc/
CONPATH=/home/gpsops/confiles/

for LFILE in `ls $*`
do
   #First a small check to ensure we have some files to work with
   LCHR=`echo $LFILE | wc -c` 
   if [ $LCHR -ne 21 -a $LCHR -ne 22 -a $LCHR -ne 12 -a $LCHR -ne 13 -a $LCHR -ne 9 -a $LCHR -ne 10 ]; then echo "$LFILE is not a valid filename, exiting..."; exit; fi
   echo "working on $LFILE..."
   case $LFILE in
   *T00) LAPBAS=`basename $LFILE .T00` ;;
   *T02) LAPBAS=`basename $LFILE .T02` ;;
   *) LAPBAS=$LFILE ;;
   esac
   SSS=`echo $LAPBAS | cut -c1-4`
   sss=`echo $SSS | tr [A-Z] [a-z]`
   CONFIL=config-${sss}
   if [ -f ${CONPATH}/${CONFIL} ] 
   then 
     echo "Found config file" > /dev/null
   else 
     echo "no config file ${CONPATH}/${CONFIL} found, exiting..."; exit 
   fi

   if [ $LCHR -eq 21 -o $LCHR -eq 22 ]; then   ## t00-file
   # Find out the preferred name...
   YEAR=`echo $LAPBAS | cut -c5-8`
   MON=`echo $LAPBAS | cut -c9-10`
   DAY=`echo $LAPBAS | cut -c11-12`
   HOUR=`echo $LAPBAS | cut -c13-14`
   DDD=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $1}'`
   SESS=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $2}'`
   YY=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $3}'`
   WEEK=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $4}'`
   RELBAS=${SSS}${DDD}${SESS}

   # extract files from the t00 file
   ${RBINDIR}/runpkr00 -dfeim $LFILE     #obsjón f ef vandraedi

   # ...and rename according to that
   /bin/mv ${LAPBAS}.dat ${RELBAS}.dat
   /bin/mv ${LAPBAS}.ion ${RELBAS}.ion
   /bin/mv ${LAPBAS}.mes ${RELBAS}.mes
   /bin/mv ${LAPBAS}.eph ${RELBAS}.eph

   elif [ $LCHR -eq 9 -o $LCHR -eq 10 ]
   then echo assuming year $YEAR
   MON=`echo $LAPBAS | cut -c5-6`
   DAY=`echo $LAPBAS | cut -c7-8`
   HOUR=`echo $LAPBAS | cut -c9 | od -t u1 | sed -n 1p | awk '{print $2-97}'`
   DDD=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $1}'`
   SESS=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $2}'`
   YY=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $3}'`
   WEEK=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $4}'`
   RELBAS=${SSS}${DDD}${SESS}

   else   ## dat-file
   DDD=`echo $LAPBAS | cut -c5-7`
   SESS=`echo $LAPBAS | cut -c8`
   echo assuming year $YEAR
   YY=`echo $YEAR | cut -c3-4`
   JD0=`$LBINDIR/juldag 20$YY 01 01 | awk '/julian/ {print $3}'`
   JD=`awk 'BEGIN {print '$JD0'+'$DDD'-1}'`
   YYYYMMDD=`$LBINDIR/juldag $JD|awk '{print $4, $5, $6}'`
   WEEK=`${LBINDIR}/lapd2dddsess.pl $YYYYMMDD 12 | awk '{print $4}'`
   RELBAS=${SSS}${DDD}${SESS}
   fi

   DATFIL=${RELBAS}.dat
   OBSFIL=${RELBAS}.${YY}O
   QCFIL=${RELBAS}.${YY}qc
   QFIL=${RELBAS}.${YY}S
   DCFIL=${RELBAS}.${YY}D.Z

   #translate data from dat/tps to rinex #and qc
   if [ $LCHR -eq 9 -o $LCHR -eq 10 ]
   #then ${TEQCPATH}/teqc -week ${WEEK} $LFILE > $OBSFIL
   #else ${TEQCPATH}/teqc -week ${WEEK} -tr d $DATFIL > $OBSFIL
   then ${TEQCPATH}/teqc $LFILE > $OBSFIL
   else ${TEQCPATH}/teqc -tr d $DATFIL > $OBSFIL
   fi
   ${TEQCPATH}/teqc +err err.lst -config ${CONPATH}/${CONFIL} $OBSFIL >tmp2.$$
   mv tmp2.$$ $OBSFIL
   ${TEQCPATH}/teqc +qc -plot $OBSFIL > $QCFIL
   /home/gpsops/bin/thjap $OBSFIL

   # move and clean up
   compress ${RELBAS}.dat ${RELBAS}.ion ${RELBAS}.mes ${RELBAS}.eph
#   rsync -uv --remove-sent-files -e ssh --timeout=30 $LFILE ${RELBAS}.dat.Z ${RELBAS}.ion.Z ${RELBAS}.mes.Z ${RELBAS}.eph.Z sil@frumgogn.vedur.is:/home/sil/gps_01/qc/$SSS/raw/
#   rsync -uv --remove-sent-files -e ssh --timeout=30 $QFIL $QCFIL sil@frumgogn.vedur.is:/home/sil/gps_01/qc/$SSS/qc/
   rsync -uv --remove-sent-files -e ssh --timeout=30 $DCFIL sil@frumgogn.vedur.is:/home/sil/gps_01/rinex/$YEAR/$SSS/
#   rm $LFILE
done 

