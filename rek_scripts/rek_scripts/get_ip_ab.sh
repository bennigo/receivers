#!/bin/sh
# Hourly and daily download of Trimble NETRS receivers at Hekla
#HG
# HG dec 2009: Use dump instead of brot
# HG jun 2011: Adapted from getmjsk.sh
#################
STATION=$1
station=`echo $STATION | tr "[:upper:]" "[:lower:]"`
sss=$station

B_FILES=0 #set to 1 to download b files also

echo "          $0 starting `date` to get data from $STATION ..."
# Check if I am already running
ps -ef > /tmp/tmpps.$$
MENUM=`grep get${station}.sh /tmp/tmpps.$$ | wc -l`
if [ $MENUM -gt 1 ]
then
  echo "A copy of get${station}.sh is already running on rek2, exiting..."
  grep get${station}.sh /tmp/tmpps.$$; rm /tmp/tmpps.$$; exit
fi
rm /tmp/tmpps.$$

# Variables:
IPNR=`grep $STATION /home/gpsops/bin/IPlist.txt | awk '{print $2}'`   # netrs ipnumber
LSDIR=/data/$STATION
LOGDIR=/home/gpsops/log/getdata/$STATION
TLOG1=$LOGDIR/hitiall.log
TLOG2=$LOGDIR/${station}.tp
VLOG1=$LOGDIR/spennaall.log
VLOG2=$LOGDIR/${station}.sp

echo "Working on $STATION, IPNR=$IPNR"

# check communications NOTE: Adapt to check if other programs may be using the link
echo "Checking connection with ping..."
IPPING=`echo $IPNR | cut -d":" -f1`
PNGCNT=`ping -c1 -w1 $IPPING |grep transmit|awk '{print $4}'`
if [ $PNGCNT -eq 0 ]
then
  echo "Receiver does not answer ping, will try again..."; sleep 3
  PNGCNT=`ping -c1 -w1 $IPNR |grep transmit|awk '{print $4}'`
  if [ $PNGCNT -eq 0 ]
  then
    echo "Receiver does not answer ping, will try again..."; sleep 3
    PNGCNT=`ping -c1 -w1 $IPNR |grep transmit|awk '{print $4}'`
    if [ $PNGCNT -eq 0 ]
    then
      echo "Receiver does not answer ping, exiting..."; 
      exit
    fi
  fi
  else
	echo The IP number $IPPING answered ping
fi
LDATE=`date '+%y%m%d %H:%M:%S'`
TSTR=`curl -m 100 'http://'$IPNR'/prog/show?temperature'`
VSTR=`curl -m 100 'http://'$IPNR'/prog/show?Voltage&input=2'`
#echo "$LDATE $TSTR" >> $TLOG1
#echo "$LDATE $TSTR" | awk '{print $1, $2, substr($4,6,2)}' >> $TLOG2
#echo "$LDATE $VSTR" >> $VLOG1
#echo "$LDATE $VSTR" | awk '{print $1, $2, substr($5,7,5)}' >> $VLOG2
# Get voltage to dump:
#tail -1 $TLOG2 > /tmp/tmp${station}.tp; tail -1 $VLOG2 > /tmp/tmp${station}.sp;
#scp /tmp/tmp${station}.tp /tmp/tmp${station}.sp sil@frumgogn:/home/sil/gps_01/logs/voltage_temp/
#ssh dump "cd /home/sil/gps_01/logs/voltage_temp; cat tmp${sss}.sp >> ${sss}.sp; cat tmp${sss}.tp >> ${sss}.tp; rm tmp${sss}.sp tmp${sss}.tp"
#rm /tmp/tmp${station}.tp /tmp/tmp${station}.sp

# Download all a- and b- pool files from the receiver
if [ $B_FILES -eq 1 ]
then
  POOLS="a b"
else
  POOLS="a"
fi

#POOLS="a b"
cd ${LSDIR}
# Get directory list 
DIRLIST=`curl -m 100 'http://'${IPNR}'/prog/show?loggedfiles&directory=/' 2>/dev/null| grep Directory | awk '{print substr($2,6,6)}' | tail -3 `
echo "There are `echo $DIRLIST | wc -w` months to check on the $STATION reciever"
#DIRLIST=201203
for P in $POOLS
do
echo "============================================"
echo "Doing pool $P"
echo "============================================"

for MDIR in $DIRLIST
do
  #echo "working on $MDIR"
  if [ -d $MDIR/$P ]
  then
    :
  else
    mkdir -p $MDIR/$P
  fi
  # get pool file list:
  curl -m 100 'http://'${IPNR}'/prog/show?loggedfiles&directory=/'${MDIR}'/'${P}''  2>/dev/null | grep name | awk '{print substr($2,6,22), substr($3,6,8)}' > tmplist.tmp
  echo "" > dlist.tmp              # clear tmp list
  # check if files have already been successfully downloaded and download new if necessary:
  FLIST=`cat tmplist.tmp | awk '{print $1}'`
  for f in $FLIST
  do 
    FSISREM=`grep $f tmplist.tmp| awk '{print $2}'`
    if [ -f $MDIR/$P/$f ]
    then
      # check size
      FSISLOC=`ls -l $MDIR/$P/$f | awk '{print $5}'`
      if [ $FSISREM -eq $FSISLOC ]
      then
        echo "$f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo "$f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..." 
        echo $f >> dlist.tmp
        mv  $MDIR/$P/$f ../dumpster/${f}.$$
      fi
    else
      echo "file $f has never been downloaded"
      echo  $f >> dlist.tmp
    fi
  done       # closes loop to check if file needs to be downloaded
  # get missing a-files for this month:
  echo "there are `wc -l dlist.tmp | awk '{print $1-1}'` $P pool files to download from the receiver for month $MDIR"
  DLIST=`cat dlist.tmp`
  for d in $DLIST
  do
    echo "getting file $d"
    pwd
    curl -m 5000 "http://${IPNR}/prog/download?loggedfile&path=/$MDIR/${P}/$d" > $MDIR/$P/$d
    # copy low sampling rate file for rinex translation:
    if [ $P = "a" ] ; then cp $MDIR/$P/$d tmp/ ; fi
    # copy file to dump? Why not just sync directory daily?
  done            # closes file loop
done            # closes month loop
done 		# closes pool loop

rm tmplist.tmp dlist.tmp
# Downloading done!

echo
#echo " Then just whip all the new files to rinex!"
cd ${LSDIR}/tmp
LIST_TO_WHIP=`ls | grep T00 | wc -l`
if [ $LIST_TO_WHIP -gt 0 ]
then
echo "there are $LIST_TO_WHIP files to whip to rinex!"
/home/gpsops/bin/mall_netrs_t00.sh *.T00
else
echo "There are no T00 files in ${LSDIR}/tmp to whip to rinex!"
fi
echo
echo "          $0 ending `date`..."
# that's it!
