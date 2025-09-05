#!/bin/sh
# translate septentrio polarx files to compressed rinex format
# usage: $0 file station,    e.g. $0 PLRX0640.07_.sbf KOSK
# dori 2007
###############

# check number of arguments etc:
NCHR=`echo $2|wc -c`
if [ $# -lt 2 -o $# -gt 2 ]; then
  echo "Usage: `basename $0` FILE STATION " 1>&2
  echo "    FILE er skráin og STATION skal vera 4-char uppercase stöðvanafnið" 1>&2
  exit 1
fi
if [ $NCHR != 5 ]; then echo "Give 4-char uppercase station name. Exiting....";
exit; fi
if [ -f $1 ]; then continue; else echo "No sbf file found, exiting..."; exit; fi
####### Variables, names and dates:
STATION=$2
station=`echo $STATION | tr "[:upper:]" "[:lower:]"`
BINPAT=/home/gpsops/bin
CONFIL=/home/gpsops/confiles/config-${station}
DOYSESYR=`echo $1 | cut -c5-11`
TMPFIL=${2}${DOYSESYR}T
OBSFIL=${2}${DOYSESYR}O
QCHFIL=${2}${DOYSESYR}Q
SBFFIL=${2}${DOYSESYR}.sbf

if [ -f ${CONFIL} ]; then continue; else echo "No config file found for site $2, exiting..."; exit; fi

###### Do the work:
${BINPAT}/sbf2rin -f $1 -d $TMPFIL
${BINPAT}/teqc +err err.lst -config ${CONFIL} $TMPFIL > $OBSFIL
${BINPAT}/thjap $OBSFIL
rm $TMPFIL

# rename sbf file if neccessary and gzip:
PNAM=`echo $1 | cut -c1-4`
if [ $PNAM = "PLRX" ]; then mv $1 $SBFFIL; gzip $SBFFIL; else gzip $1; fi


