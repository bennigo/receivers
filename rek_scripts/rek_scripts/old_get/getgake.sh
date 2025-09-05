#!/bin/sh
# Download all missing septentrio GPS data except current file
# Assume current file (the file being written to) is at the bottom of the file list. Hope this is true...
# dori aug 2007
# add check for size, dori sep 2007
# "current file" is not always last file in list. Use dates for identification [dori nov 2007]
# Add check if I am running [dori nov 2007]
# Use dump instead of brot [HG dec 2009]
# Updated to elegance [Fjalar 2014]
#################

STATION=GAKE
echo "        Starting to retrieve data from $STATION at `date`..."

IP=157.157.222.110
PORT=":8080"

# two intuicom with unassigned ip numbers connect GAKE to the Kopasker academic center

cd /data/$STATION/15sec

# check if I am already running
ps -ef > /tmp/tmpps.$$
MENUM=`grep getgake.sh /tmp/tmpps.$$ | wc -l`
if [ $MENUM -gt 1 ]
then
  echo "A copy of rek: getgake.sh is already running on rek, you must wait or check for hanging processes. Exiting..."
  grep getgake.sh /tmp/tmpps.$$; rm /tmp/tmpps.$$; exit
fi
rm /tmp/tmpps.$$

echo "Checking connection..."
PNGCNT=`ping -c1 -w1 $IP |grep transmit|awk '{print $4}'`
if [ $PNGCNT -eq 0 ]
then
  echo "Receiver does not answer ping, will try again..."; sleep 3
  PNGCNT=`ping -c1 -w3 $IP |grep transmit|awk '{print $4}'`
  if [ $PNGCNT -eq 0 ]
  then
    echo "Receiver does not answer ping, will try again..."; sleep 3
    PNGCNT=`ping -c1 -w7 $IP |grep transmit|awk '{print $4}'`
    if [ $PNGCNT -eq 0 ]
    then
      echo "Receiver $IP does not answer ping, exiting..."; exit
    fi
  fi
fi

# get file list, prepare format and remove current file from list, defined as file with same day as cpu date
echo "get file list and prepare format..."
TDAY=`date '+%b-%d-%Y'`
curl 'http://'$IP$PORT'/cgi-bin/anycmd?anycmd=gfl' | grep PLRX | grep -v $TDAY | awk '{print substr($5,2,16), $4}' > flist.tmp

# check if only 1 or less files are on receiver:
FCNT=`wc -l flist.tmp| awk '{print $1}'`

if [ "$FCNT" -lt 2 ]; then echo "No file to download (too few files on receiver), exiting...  (filelist:)"; cat flist.tmp; rm flist.tmp; exit 0; fi

# Check if files have been downloaded, check for size and prepare format
FLIST=`cat flist.tmp | awk '{printf "%s ", $1}'`
echo "" > dlist.tmp
for f in $FLIST
do
  if [ -f $f ]
  then
    FSIZST=`grep $f flist.tmp | awk '{print $2}'`
    FSIZPC=`ls -l $f | awk '{print $5}'`
    if [ $FSIZST -eq $FSIZPC ]
    then
      echo "$f has already been successfully downloaded"
    else
      echo "$f has already been downloaded but some bytes seem to be missing ($FSIZPC of $FSIZST downloaded), will try again..."      echo $f >> dlist.tmp
      mv $f ../dumpster/${f}.$$
    fi
  else
    echo $f >> dlist.tmp
  fi
done

# get missing files
echo "there are `grep PLRX dlist.tmp | wc -l | awk '{print $1}'` files to download from the receiver"
DLIST=`cat dlist.tmp | awk '{printf "%s ", $1}'`
for d in $DLIST
do
  echo "getting file $d"
  wget http://${IP}${PORT}/log/disk1/$d
  cp $d /data/$STATION/translate/
done
rm dlist.tmp flist.tmp

echo "translating sbf to hc rinex..."
cd /data/$STATION/translate/
FNUM=`ls *.sbf | wc -l`
F2TR=`ls *.sbf`

echo "Number of files to translate: " $FNUM
echo "Files to translate" $F2TR

if [ "$FNUM" -gt 1 ]
then
for f in `ls *.sbf`
do
  DYR=`echo $f | cut -c5-11`
  YEAR=20`echo $f | cut -c10-11`
  DCFIL=${STATION}${DYR}D.Z
  sh /home/gpsops/bin/sbf2crx.sh $f $STATION
  rsync ${DCFIL} sil@frumgogn:/home/sil/gps_01/rinex/$YEAR/$STATION/ 
  mv ${STATION}*gz ../15sec/save/
done
fi
echo "        Done retrieving data from $STATION at `date`..."; echo ""

