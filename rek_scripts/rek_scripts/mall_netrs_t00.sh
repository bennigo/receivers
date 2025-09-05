#!/bin/sh
# convert t00/T00 data in batches from cgps sites
# HG feb 2007 (based on older stuff)
# HG sep 2007: Adopt from brot to rek
# HG dec 2009: Use frumgogn instead of brot 
######################
RBINDIR=/home/gpsops/bin/
LBINDIR=/home/gpsops/bin
TEQCPATH=/home/gpsops/bin/teqc/
CONPATH=/home/gpsops/confiles/tmp
for LFILE in `ls $*`
do
   #First a small check to ensure we have some files to work with
   LCHR=`echo $LFILE | wc -c` 
   if [ $LCHR -lt 20 ]; then echo "$LFILE is not a valid filename, exiting..."; exit; fi
   echo "working on $LFILE..."
   LAPBAS=`basename $LFILE .T00`
   #SSS=`echo $LAPBAS | cut -c0-4`
   SSS=`echo $LAPBAS | cut -c1-4`
	echo $SSS
   sss=`echo $SSS | tr [A-Z] [a-z]`
   CONFIL=config-${sss}
   if [ -f ${CONPATH}${CONFIL} ] 
   then 
     echo "Found config file" > /dev/null
   else 
     echo "no config file ${CONPATH}${CONFIL} found, exiting..."; exit 
   fi
   # Find out the preferred name...
   YEAR=`echo $LAPBAS | cut -c5-8`
   MON=`echo $LAPBAS | cut -c9-10`
   DAY=`echo $LAPBAS | cut -c11-12`
   HOUR=`echo $LAPBAS | cut -c13-14`
	echo "$SSS $sss $YEAR $MON $DAY $HOUR"
   DDD=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $1}'`
   SESS=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $2}'`
   YY=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $3}'`
   WEEK=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $4}'`
   RELBAS=${SSS}${DDD}${SESS}

   # extract files from the t00 file
   ${RBINDIR}runpkr00 -dfeim $LFILE     #obsjón f ef vandræði

   DATFIL=${RELBAS}.dat
   OBSFIL=${RELBAS}.${YY}O
   QCFIL=${RELBAS}.${YY}qc
   QFIL=${RELBAS}.${YY}S
   DCFIL=${RELBAS}.${YY}D.Z

   # ...and rename according to that
   /bin/mv ${LAPBAS}.dat ${RELBAS}.dat
   /bin/mv ${LAPBAS}.ion ${RELBAS}.ion
   /bin/mv ${LAPBAS}.mes ${RELBAS}.mes
   /bin/mv ${LAPBAS}.eph ${RELBAS}.eph

   #translate data from dat to rinex #and qc
   #${TEQCPATH}/teqc_2006 -week ${WEEK} -tr d $DATFIL > $OBSFIL
   #${TEQCPATH}/teqc_2006 +err err.lst -config ${CONPATH}/${CONFIL} $OBSFIL >tmp2.$$
   #${TEQCPATH}/teqc -week ${WEEK} -tr d $DATFIL > $OBSFIL
   ${TEQCPATH}/teqc -tr d $DATFIL > $OBSFIL
   ${TEQCPATH}/teqc +err err.lst -config ${CONPATH}/${CONFIL} $OBSFIL >tmp2.$$

   mv tmp2.$$ $OBSFIL
   #${TEQCPATH}/teqc_2006 +qc -plot $OBSFIL > $QCFIL
   ${TEQCPATH}/teqc +qc -plot $OBSFIL > $QCFIL

   /home/gpsops/bin/thjap $OBSFIL

   # move and clean up
   compress ${RELBAS}.dat ${RELBAS}.ion ${RELBAS}.mes ${RELBAS}.eph
   echo "move all the files to frumgogn.vedur.is, you can find them there at: " 
   echo "/home/sil/gps_01/qc/$SSS/raw,  /home/sil/gps_01/qc/$SSS/qc and at /home/sil/gps_01/rinex/$YEAR/$SSS"
   #rsync -uv $LFILE ${RELBAS}.dat.Z ${RELBAS}.ion.Z ${RELBAS}.mes.Z ${RELBAS}.eph.Z sil@frumgogn:/home/sil/gps_01/qc/$SSS/raw/
   #rsync -uva $QFIL $QCFIL sil@frumgogn:/home/sil/gps_01/qc/$SSS/qc/
   rsync -uva $DCFIL sil@frumgogn:/home/sil/gps_01/rinex/$YEAR/$SSS/
   rm $LFILE
done 
