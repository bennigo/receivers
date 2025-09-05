#!/bin/sh
# Hourly and daily download of Trimble NETR9 receiver RHOF
# SFS jun 2011: Adapted from getsoho.sh to download from AUST which now has 3G conection.
# SFS Sept 2011: Adapted to download b-files.
#################

# ADAPTION FOR NEW STATIONS
#
# When this script is adapted for new/other stations the following has to be changed:
# (1) Create the paths marked with #CREATE# on rek2
# (2) Touch the files marked with #TOUCH# on rek2
# (3) Adapt messages as needed, where #ADAPT# is a tag.
# (4) Create the station data folder under /data/{STATION}, e.g. /data/isgpslog/SKOG
# 
# This should be it.


# Variables:
STATION_FULL_NAME="Raufarhöfn"
STATION=RHOF
station=rhof

IP=157.157.222.107       		# netr9 web access and programable interface
HTTP_PORT=':8080'				# HTTP
#FTP_PORT=':2160'				# FTP

LSDIR=/data/$STATION          #CREATE# 
LOGDIR=/home/gpsops/log/getdata/$STATION  #CREATE# 
TMPDIR=/data/$STATION/tmp	#CREATE# A-files
TMPDIR1=/data/$STATION/tmp1   #CREATE# B-files


TLOG1=$LOGDIR/hitiall.log		#TOUCH#
TLOG2=$LOGDIR/${station}.tp		#TOUCH#	
VLOG1=$LOGDIR/spennaall.log		#TOUCH#
VLOG2=$LOGDIR/${station}.sp		#TOUCH#

MONTHS=2
B_FILES=1

MESSAGE="-- > 3G modem"                 #ADAPT#

# Welcome messagee

echo " $0 starting..."
echo " "
echo "This is a download script for $STATION_FULL_NAME ($STATION) IP:$IP"
echo `date`
echo " "


# Check if I am already running
echo "Checking other running get${station}.sh processes:"

ps -ef > /tmp/tmpps.$$

MENUM=`grep get${station}.sh /tmp/tmpps.$$ | wc -l`
MENUM2=`grep viblablabla /tmp/tmpps.$$ | wc -l`

if [ $MENUM -gt 1 ]
then
  if [ $MENUM2 -gt 0 ]
  then 
   echo "-- > get${station}.sh is being edited with vi.. OK"
   echo " "
  else 
   echo "-- > A copy of get${station}.sh is already running on rek2, exiting..."
   grep get${station}.sh /tmp/tmpps.$$; rm /tmp/tmpps.$$; exit
  fi
else
   echo "" 
fi
rm /tmp/tmpps.$$


# Check if the receiver is on-line

echo "Checking if the modem and receiver is live and kicking:"
PNGCNT=`ping -c1 -w1 $IP |grep transmit|awk '{print $4}'`
  if [ $PNGCNT -eq 0 ]
  then
    echo "-- > Ping check 1: Does not answer ping..."
    echo "-- > Will try again.."
    sleep 3
    PNGCNT=`ping -c1 -w1 $IP |grep transmit|awk '{print $4}'`

    if [ $PNGCNT -eq 0 ]
    then
       echo "-- > Ping check 2: Does not answer ping..."
       echo "-- > Will try again.."
       sleep 3
       PNGCNT=`ping -c1 -w1 $IP |grep transmit|awk '{print $4}'`
       
       if [ $PNGCNT -eq 0 ]
       then
          echo "-- > Ping check 3: Does not answer ping..."
          echo "-- > Ok, I give up! Try again later.."
          echo -e "-- > Process aborted."; exit 
       else
         echo -e $MESSAGE "\t(" $IP ") : Answers ping!"
       fi
     else
       echo -e $MESSAGE "\t(" $IP ") : Answers ping!"
     fi
  else
     echo -e $MESSAGE "\t(" $IP ") : Answers ping!"
  fi

# Get temperature and voltage information

echo ""
echo "Fetching temperature and voltage information:"
LDATE=`date '+%y%m%d %H:%M:%S'`
echo "-- > Fetching temperature..."
TSTR=`curl 'http://'$IP$HTTP_PORT'/prog/show?temperature'`
echo "-- > " $TSTR
echo "-- > Fetching voltage..."
VSTR=`curl 'http://'$IP$HTTP_PORT'/prog/show?voltages' | grep port`
echo "-- > " $VSTR
#echo "$LDATE $TSTR" >> $TLOG1
#echo "$LDATE $TSTR" | awk '{print $1, $2, substr($4,6,2)}' >> $TLOG2
#echo "$LDATE $VSTR" >> $VLOG1
#echo "$LDATE $VSTR" | awk '{print $1, $2, substr($5,7,5)}' >> $VLOG2

# mirror the receiver!

echo " "
echo "Mirroring the receiver:"
cd ${LSDIR}

# Get directory list for 15 sec files for the last two months:
echo "-- > Getting directory list..."
DIRLIST=`curl 'http://'$IP$HTTP_PORT'/prog/show?directory&path=/Internal/' 2>/dev/null|  grep 'directory name' | grep -v 'lost' | awk '{print substr($2,6,6)}' | grep -v '30s_8h' | tail -${MONTHS}`
echo "--> DIRECTORIES: $DIRLIST"
#DIRLIST=201101
for MDIR in $DIRLIST
do
  cd ${LSDIR}
  echo "-- > Working on $MDIR"
  if [ -d $MDIR ]
  then
    :
  else
    mkdir $MDIR; mkdir $MDIR/a; mkdir $MDIR/b
    #mkdir $MDIR
  fi

  # get a-file list:
  echo " "
  echo "-- > Getting a-file list..."
  curl 'http://'$IP$HTTP_PORT'/prog/show?directory&path=/Internal/'${MDIR}'/15s_24h'  2>/dev/null | grep name | grep -v .T0B | awk '{print substr($2,6,28), substr($3,6,8)}' | grep ${STATION} > tmplist.tmp

  # check if more than one file NEED TO ADD IF LOOP
  echo "" > dlist.tmp              # clear tmp list
  
  # check if files have already been successfully downloaded and download new if necessary:
  echo " "
  echo "-- > Cheking if files have been successfully downloaded..."
  FLIST=`cat tmplist.tmp | awk '{print $1}'`
  for f in $FLIST
  do 
    FSISREM=`grep $f tmplist.tmp| awk '{print $2}'`
    if [ -f $MDIR/a/$f ]
    #if [ -f $MDIR/$f ]
    then
      # check size
      FSISLOC=`ls -l $MDIR/a/$f | awk '{print $5}'`
      #FSISLOC=`ls -l $MDIR/$f | awk '{print $5}'`
      if [ $FSISREM -eq $FSISLOC ]
      then
        echo "-- > $f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo "-- > $f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..." 
        echo $f >> dlist.tmp
        mv  $MDIR/a/$f ../dumpster/${f}.$$
        #mv  $MDIR/$f ../dumpster/${f}.$$
      fi
    else
      echo "-- > File $f has never been downloaded"
      echo  $f >> dlist.tmp
    fi
  done       # closes loop to check if file needs to be downloaded


  # get missing a-files for this month:
  echo "Getting missing a-files for $MDIR:"
  echo "-- > There are `grep $STATION dlist.tmp | wc -l | awk '{print $1}'` 15 sec files to download from the receiver for month $MDIR"
  DLIST=`cat dlist.tmp`
  for d in $DLIST
  do
    echo "-- > Getting file $d"
    cd $LSDIR/$MDIR/a
    curl "http://$IP$HTTP_PORT/prog/download?file&path=/Internal/$MDIR/15s_24h/$d" > $d
  done            # closes file loop
#done            # closes month loop


##### B-files #####

if [ $B_FILES -eq 1 ]
then
  # get b-file list:
  echo " "
  echo "-- > Getting b-file list..."
  cd $LSDIR
  curl 'http://'$IP$HTTP_PORT'/prog/show?directory&path=/Internal/'${MDIR}'/1Hz_1h'  2>/dev/null | grep name | grep -v .T0B | awk '{print substr($2,6,28), substr($3,6,8)}' | grep ${STATION} > tmplist1.tmp

  # check if more than one file NEED TO ADD IF LOOP
  echo "" > dlist1.tmp              # clear tmp list
  
  # check if files have already been successfully downloaded and download new if necessary:
  echo " "
  echo "-- > Cheking if files have been successfully downloaded..."
  FLIST=`cat tmplist1.tmp | awk '{print $1}'`
  for f in $FLIST
  do 
    FSISREM=`grep $f tmplist1.tmp| awk '{print $2}'`
    if [ -f $MDIR/b/$f ]
    then
      # check size
      FSISLOC=`ls -l $MDIR/b/$f | awk '{print $5}'`
      if [ $FSISREM -eq $FSISLOC ]
      then
        echo "-- > $f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo "-- > $f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..." 
        echo $f >> dlist1.tmp
        mv  $MDIR/b/$f ../dumpster/${f}.$$
      fi
    else
      echo "-- > File $f has never been downloaded"
      echo  $f >> dlist1.tmp
    fi
  done       # closes loop to check if file needs to be downloaded


  # get missing b-files for this month:
  echo "Getting missing b-files for $MDIR:"
  echo "-- > There are `grep $STATION dlist1.tmp | wc -l | awk '{print $1}'` 1 sec files to download from the receiver for month $MDIR"
  DLIST=`cat dlist1.tmp`
  for d in $DLIST
  do
    echo "-- > Getting file $d"
    cd $LSDIR/$MDIR/b
    #wget -c ftp://${IPNR}/Internal/$MDIR/1Hz_1h/$d
    curl "http://$IP$HTTP_PORT/prog/download?file&path=/Internal/$MDIR/1Hz_1h/$d" > $d
  done            # closes file loop 
 fi
done            # closes month loop



cd $LSDIR
rm tmplist.tmp dlist.tmp tmplist1.tmp dlist1.tmp
# mirroring done!
echo "-- > Mirroring complete!"
echo ""

#### RINEX ####

#echo "RINEX transform is disabled at the moment.."
# transform the "latestish" files to rinex and move to correct places...
# First build a file list of existing 24hr T00 files i.e. dump new files into tmp directory:
echo ""
echo "Transforming files to RINEX:"
cd $LSDIR
echo "--> Building a list of files..."
ls */a/*a.T02 > currlist.lst
ls */b/*b.T02 > currlist1.lst

echo "--> A-files"
for f in `cat currlist.lst`
do
  # see if files have already been transformed to rinex
  DFLAG=`grep $f donelist.lst | wc -l`
  if [ $DFLAG -eq 0 ]
  then
    echo "-- > Files that will be transformed: " $DFLAG
    # file has not been transformed
    cp $f $TMPDIR
    echo $f >> donelist.lst
  fi
done

# Then just whip all the new files to rinex!
echo "-- > Transforming files..."
echo " "
cd /data/${STATION}/tmp
/home/gpsops/bin/mall_netr9_t02.sh *.T02


cd $LSDIR
echo "--> B-files"
for f in `cat currlist1.lst`
do
  # see if files have already been transformed to rinex
  DFLAG=`grep $f donelist1.lst | wc -l`
  if [ $DFLAG -eq 0 ]
  then
    echo "-- > Files that will be transformed: " $DFLAG
    # file has not been transformed
    cp $f $TMPDIR1
    echo $f >> donelist1.lst
  fi
done

# Then just whip all the new files to rinex!
echo "-- > Transforming files..."
echo " "
cd /data/${STATION}/tmp1
/home/gpsops/bin/mall_netr9_1hz_t02.sh *.T02

echo "-- > $0 ending `date`..."

# that's it!
