#!/bin/sh
# convert t00/T00 data in batches from cgps sites
# HG feb 2007 (based on older stuff)
# HG sep 2007: Adopt from brot to rek
# HG dec 2009: Use frumgogn instead of brot 
# SFS jul 2011: Adapted for NetR9
######################

# Some variables and paths

RBINDIR=/home/gpsops/bin/
LBINDIR=/home/gpsops/bin
TEQCPATH=/home/gpsops/bin/
CONPATH=/home/gpsops/confiles/

for LFILE in `ls $*`
do
   #First a small check to ensure we have some files to work with
   LCHR=`echo $LFILE | wc -c` 
   if [ $LCHR -lt 20 ]; then echo "$LFILE is not a valid filename, exiting..."; exit; fi
   echo "working on $LFILE..."
   LAPBAS=`basename $LFILE .T00`
   echo $LAPBAS
   #SSS=`echo $LAPBAS | cut -c0-4`
   SSS=`echo $LAPBAS | cut -c1-4`
   echo $SSS
   sss=`echo $SSS | tr [A-Z] [a-z]`
   echo $sss
   
   CONFIL=config-${sss}
   #echo $CONFIL
   
   if [ -f ${CONPATH}${CONFIL} ] 
   then 
     #echo "Found config file" > /dev/null
     echo ">> Found config file ${CONPATH}${CONFIL}"
   else 
     echo "no config file ${CONPATH}${CONFIL} found, exiting..."; exit 
   fi
   
   # Find out the preferred name... 
  # if [ $SSS == AUST ] || [ $SSS = FIM2 ] || [ $SSS = ENTA ] || [ $SSS = HAFS ] || [ $SSS = OFEL ] # This is to handle names that have "____" underscores in them... 
   #then 
   #  YEAR=`echo $LAPBAS | cut -c11-14`
   #  MON=`echo $LAPBAS | cut -c15-16`
   #  DAY=`echo $LAPBAS | cut -c17-18`
   #  HOUR=`echo $LAPBAS | cut -c19-20`
  # 
  # else
     YEAR=`echo $LAPBAS | cut -c5-8`
     MON=`echo $LAPBAS | cut -c9-10`
     DAY=`echo $LAPBAS | cut -c11-12`
     HOUR=`echo $LAPBAS | cut -c13-14`
   #fi


   echo "$SSS $sss $YEAR $MON $DAY $HOUR"
   DDD=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $1}'`
   SESS=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $2}'`
   YY=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $3}'`
   WEEK=`${LBINDIR}/lapd2dddsess.pl $YEAR $MON $DAY $HOUR | awk '{print $4}'`
   zero=0
   if [ ${SESS} -eq $zero ]
   then
        SESS=a
   fi
   echo $RELBAS
   RELBAS=${SSS}${DDD}${SESS}

   # extract files from the t00 file
   ${RBINDIR}runpkr00 -dfeim $LFILE     #obsjón f ef vandræði

   DATFIL=${RELBAS}.dat
   OBSFIL=${RELBAS}.${YY}O
   QCFIL=${RELBAS}.${YY}qc
   QFIL=${RELBAS}.${YY}S
   DCFIL=${RELBAS}.${YY}D.Z
   Final=${RELBAS}.${YY}D
   Final=`echo ${Final} | tr [:upper:] [:lower:]`

   # ...and rename according to that
   /bin/mv ${LAPBAS}.dat ${RELBAS}.dat
   /bin/mv ${LAPBAS}.ion ${RELBAS}.ion
   /bin/mv ${LAPBAS}.mes ${RELBAS}.mes
   /bin/mv ${LAPBAS}.eph ${RELBAS}.eph
   
   sampl="-O.int 30 -O.dec 30"
   #translate data from dat to rinex #and qc
   echo $DATFIL
   ${TEQCPATH}teqc ${sampl} -week ${WEEK} -tr d $DATFIL  > $OBSFIL   # +C2 added for NetR9/T02 data
   ${TEQCPATH}teqc +err err.lst -config ${CONPATH}/${CONFIL} $OBSFIL >tmp2.$$
   mv tmp2.$$ $OBSFIL
   ${TEQCPATH}teqc +qc -plot $OBSFIL > $QCFIL
   /home/gpsops/bin/thjap $OBSFIL


   echo $REALBAS
   
   # move and clean up
   rm ${RELBAS}.dat ${RELBAS}.ion ${RELBAS}.mes ${RELBAS}.eph 

   mv $DCFIL ../rinex/${Final}.Z
   #rsync -uva $DCFIL sil@frumgogn:/home/sil/gps_01/rinex/$YEAR/$SSS/1hz
  #rm $LFILE
done
#rm * 
