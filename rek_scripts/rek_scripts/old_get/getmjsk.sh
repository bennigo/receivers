#!/bin/sh
# Hourly and daily download of Trimble NETRS receiver MJSK
#HG
# HG dec 2009: Use dump instead of brot
#################
STATION=MJSK
station=mjsk
sss=mjsk
echo "          $0 starting `date`..."
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
#IPNR=130.208.196.115   # netrs ipnumber
IPNR=130.208.196.45:8080   # netrs ipnumber
USER=gps
PASSW=katla1918


LSDIR=/data/$STATION
LOGDIR=/home/gpsops/log/getdata/$STATION
TLOG1=$LOGDIR/hitiall.log
TLOG2=$LOGDIR/${station}.tp
VLOG1=$LOGDIR/spennaall.log
VLOG2=$LOGDIR/${station}.sp

MONTH=2
B_FILES=1

# check communications - ping virkar á MJSK!
#  echo "Checking connection with ping..."
#  PNGCNT=`ping -c1 -w1 $IPNR |grep transmit|awk '{print $4}'`
#  if [ $PNGCNT -eq 0 ]
#  then
#    echo "Receiver does not answer ping, will try again..."; sleep 3
#    PNGCNT=`ping -c1 -w1 $IPNR |grep transmit|awk '{print $4}'`
#    if [ $PNGCNT -eq 0 ]
#    then
#      echo "Receiver does not answer ping, will try again..."; sleep 3
#      PNGCNT=`ping -c1 -w1 $IPNR |grep transmit|awk '{print $4}'`
#      if [ $PNGCNT -eq 0 ]
#      then
#        echo "Receiver does not answer ping, exiting..."; 
#        exit
#      fi
#    fi
#  fi

LDATE=`date '+%y%m%d %H:%M:%S'`
TSTR=`curl -u silgps:katla1918 'http://'$IPNR'/prog/show?temperature'`
VSTR=`curl -u silgps:katla1918 'http://'$IPNR'/prog/show?Voltage&input=1'`
echo "Temperature: "
echo ">> "$TSTR
echo "Voltage: "
echo ">> "$VSTR
##echo "$LDATE $TSTR" >> $TLOG1
##echo "$LDATE $TSTR" | awk '{print $1, $2, substr($4,6,2)}' >> $TLOG2
##echo "$LDATE $VSTR" >> $VLOG1
##echo "$LDATE $VSTR" | awk '{print $1, $2, substr($5,7,5)}' >> $VLOG2
# Get voltage to dump:

#tail -1 $TLOG2 > /tmp/tmp${station}.tp; tail -1 $VLOG2 > /tmp/tmp${station}.sp;
##scp /tmp/tmp${station}.tp /tmp/tmp${station}.sp sil@frumgogn:/home/sil/gps_01/logs/voltage_temp/
##ssh dump "cd /home/sil/gps_01/logs/voltage_temp; cat tmp${sss}.sp >> ${sss}.sp; cat tmp${sss}.tp >> ${sss}.tp; rm tmp${sss}.sp tmp${sss}.tp"
#rm /tmp/tmp${station}.tp /tmp/tmp${station}.sp

# mirror the receiver!
cd ${LSDIR}

echo "Getting directory list for all sessions:"
DIRLIST=`curl -u $USER:$PASSW 'http://'${IPNR}'/prog/show?loggedfiles&directory=/' 2>/dev/null|  grep Directory | awk '{print substr($2,6,6)}' | tail -${MONTHS}`
echo "Directories: "$DIRLIST
for MDIR in $DIRLIST
do
  echo "working on $MDIR"
  if [ -d $MDIR ]
  then
    :
  else
    mkdir $MDIR; mkdir $MDIR/a; mkdir $MDIR/b
  fi
  # get a-file list:
  curl -u $USER:$PASSW 'http://'${IPNR}'/prog/show?loggedfiles&directory=/'${MDIR}'/a'  2>/dev/null | grep name | awk '{print substr($2,6,22), substr($3,6,8)}' > tmplist.tmp
  # check if more than one file NEED TO ADD IF LOOP
  echo "" > dlist.tmp              # clear tmp list
  # check if files have already been successfully downloaded and download new if necessary:
  FLIST=`cat tmplist.tmp | awk '{print $1}'`
  for f in $FLIST
  do 
    FSISREM=`grep $f tmplist.tmp| awk '{print $2}'`
    if [ -f $MDIR/a/$f ]
    then
      # check size
      FSISLOC=`ls -l $MDIR/a/$f | awk '{print $5}'`
      if [ $FSISREM -eq $FSISLOC ]
      then
        echo "$f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo "$f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..." 
        echo $f >> dlist.tmp
        mv  $MDIR/a/$f ../dumpster/${f}.$$
      fi
    else
      echo "file $f has never been downloaded"
      echo  $f >> dlist.tmp
    fi
  done       # closes loop to check if file needs to be downloaded
  # get missing a-files for this month:
  echo "there are `grep MJSK dlist.tmp | wc -l | awk '{print $1}'` 15 sec files to download from the receiver for month $MDIR"
  DLIST=`cat dlist.tmp`
  for d in $DLIST
  do
    echo "getting file $d"
    curl -u $USER:$PASSW 'http://'${IPNR}'/prog/download?loggedfile&path=/'$MDIR'/a/'$d''  > $MDIR/a/$d
  done            # closes file loop
done            # closes month loop

# and now get the b-files
echo "" > dlist.tmp
for MDIR in $DIRLIST
do
  # get b-file list:
  curl -u $USER:$PASSW 'http://'${IPNR}'/prog/show?loggedfiles&directory=/'${MDIR}'/b'  2>/dev/null | grep name | awk '{print substr($2,6,22), substr($3,6,8)}' > tmplist.tmp
  # check if files have already been successfully downloaded and download new if necessary:
  FLIST=`cat tmplist.tmp | awk '{print $1}'`
  for f in $FLIST
  do
    FSISREM=`grep $f tmplist.tmp| awk '{print $2}'`
    if [ -f $MDIR/b/$f ]
    then
      # check size
      FSISLOC=`ls -l $MDIR/b/$f | awk '{print $5}'`
      if [ $FSISREM -eq $FSISLOC ]
      then
        echo "$f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo "$f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..."
        echo $f >> dlist.tmp
        mv  $MDIR/b/$f ../dumpster/${f}.$$
      fi
    else
      echo "file $f has never been downloaded"
      echo  $f >> dlist.tmp
    fi
  done     # closes loop to check if files need to be downloaded
  # get missing b-files:
  echo "there are `grep MJSK dlist.tmp | wc -l | awk '{print $1}'` 1 sec files to download from the receiver for month $MDIR"
  DLIST=`cat dlist.tmp`
  for d in $DLIST
  do
    echo "getting file $d"
    curl -u $USER:$PASSW 'http://'${IPNR}'/prog/download?loggedfile&path=/'$MDIR'/b/'$d'' > $MDIR/b/$d
  done                  # closes doenload loop
done                #closes month loop

rm tmplist.tmp dlist.tmp
# mirroring done!

# transform the "latestish" files to rinex and move to correct places...
# First build a file list of existing 24hr T00 files i.e. dump new files into tmp directory:
ls */a/*a.T00 > currlist.lst
for f in `cat currlist.lst`
do
  # see if files have already been transformed to rinex
  DFLAG=`grep $f donelist.lst | wc -l`
  if [ $DFLAG -eq 0 ]
  then
    # file has not been transformed
    cp $f tmp/
    echo $f >> donelist.lst
  fi
done

# Then just whip all the new files to rinex!
cd /data/${STATION}/tmp
/home/gpsops/bin/mall_netrs_t00.sh *.T00
echo "          $0 ending `date`..."
# that's it!
