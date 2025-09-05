#!/bin/sh
# Hourly and daily download of Trimble NETRS receiver KIDJ
# SFS jun 2011: Adapted from getsoho.sh to download from KIDJ which now has 3G conection.
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

STATION=KIDJ
STATION_FULL_NAME="Kiðjaberg"
station=kidj
SSS=KIDJ
sss=kidj

IPNR=157.157.145.126	     		# netrs ipnumber 
IPNRFTP=157.157.145.126	   		# netrs ipnumber
PORT=8060

LSDIR=/data/$STATION          #CREATE# 
LOGDIR=/home/gpsops/log/getdata/$STATION  #CREATE# 
TMPDIR=/data/$STATION/tmp	#CREATE#
TMPDIR1=/data/$STATION/tmp1   #CREATE#

TLOG1=$LOGDIR/hitiall.log		#TOUCH#
TLOG2=$LOGDIR/${station}.tp		#TOUCH#	
VLOG1=$LOGDIR/spennaall.log		#TOUCH#
VLOG2=$LOGDIR/${station}.sp		#TOUCH#

USER=silgps
PWS=katla1918

B_FILES=1
MONTHS=12

# Different global messages
MESSAGE=">> Modem"                 #ADAPT#
ABORT_MESSAGE="[Process aborted]"
OK_MESSAGE="[OK]"

# Welcome messagee

echo " "
echo " $0 starting..."
echo " "
echo "#######################################################################"
echo " "
echo "This is a download script for $STATION_FULL_NAME ($STATION) IP:$IPNRFTP"
echo `date`
echo " "


# Check if I am already running
echo "## Checking other running get${station}.sh processes ##"
echo ""

ps -ef > /tmp/tmpps.$$

MENUM=`grep get${station}.sh /tmp/tmpps.$$ | wc -l`
MENUM2=`grep viblablabla /tmp/tmpps.$$ | wc -l`

if [ $MENUM -gt 1 ]
then
  if [ $MENUM2 -gt 0 ]
  then 
   echo ">> get${station}.sh is being edited with vi: $OK_MESSAGE"
   echo " "
  else 
   echo ">> A copy of get${station}.sh is already running on rek2: $ABORT_MESSAGE"
   grep get${station}.sh /tmp/tmpps.$$; rm /tmp/tmpps.$$; exit
  fi
else
   echo ">> No other get${station}.sh is running on rek2: $OK_MESSAGE" 
fi
rm /tmp/tmpps.$$


# Check if the receiver is on-line

echo ""
echo "## Checking if the modem and receiver is live and kicking ##"
echo ""

PNGCNT=`ping -c1 -w1 $IPNRFTP |grep transmit|awk '{print $4}'`
  if [ $PNGCNT -eq 0 ]
  then
    echo ">> Ping check #1: Does not answer ping"
    echo ">> Will try again"
    sleep 3
    PNGCNT=`ping -c3 -w3 $IPNRFTP |grep transmit|awk '{print $4}'`

    if [ $PNGCNT -eq 0 ]
    then
       echo ">> Ping check #2: Does not answer ping"
       echo ">> Will try again"
       sleep 3
       PNGCNT=`ping -c5 -w5 $IPNRFTP |grep transmit|awk '{print $4}'`
       
       if [ $PNGCNT -eq 0 ]
       then
          echo ">> Ping check #3: Does not answer ping"
          echo -e ">> No ping from modem $ABORT_MESSAGE"; exit 
       else
         echo -e $MESSAGE " answers ping" $OK_MESSAGE
       fi
     else
       echo -e $MESSAGE " answers ping" $OK_MESSAGE
     fi
  else
     echo -e $MESSAGE " answers ping" $OK_MESSAGE
  fi

# Get temperature and voltage information

echo ""
echo "## Fetching temperature and voltage information ##"
echo ""

LDATE=`date '+%y%m%d %H:%M:%S'`
TSTR=`curl -u ${USER}:${PWS} 'http://'$IPNR':'$PORT'/prog/show?temperature'`
echo ">> "$TSTR
VSTR=`curl -u ${USER}:${PWS} 'http://'$IPNR':'$PORT'/prog/show?Voltage&input=2'`
echo ">> "$VSTR

#echo "$LDATE $TSTR" >> $TLOG1
#echo "$LDATE $TSTR" | awk '{print $1, $2, substr($4,6,2)}' >> $TLOG2
#echo "$LDATE $VSTR" >> $VLOG1
#echo "$LDATE $VSTR" | awk '{print $1, $2, substr($5,7,5)}' >> $VLOG2

####### mirror the receiver! #######

echo " "
echo "## Mirroring the receiver ##"
echo ""

cd ${LSDIR}
# Get directory list for 15 sec files for the last two months:
echo ">> Getting directory list"
DIRLIST=`curl -u ${USER}:${PWS} 'http://'${IPNR}':'${PORT}'/prog/show?loggedfiles&directory=/' 2>/dev/null|  grep Directory | awk '{print substr($2,6,6)}' | tail -$MONTHS`
for MDIR in $DIRLIST
do
  cd ${LSDIR}
  echo ">> Working on month $MDIR"
  if [ -d $MDIR ]
  then
    :
  else
    mkdir $MDIR; mkdir $MDIR/a; mkdir $MDIR/b
  fi

######## get a-file list:  #########
  echo ">> Getting a-file list"
  curl -u ${USER}:${PWS} 'http://'${IPNR}':'${PORT}'/prog/show?loggedfiles&directory=/'${MDIR}'/a'  2>/dev/null | grep name | awk '{print substr($2,6,22), substr($3,6,8)}' | grep ${STATION} > tmplist.tmp

  # check if more than one file NEED TO ADD IF LOOP
  echo "" > dlist.tmp              # clear tmp list
  
  # check if files have already been successfully downloaded and download new if necessary:
  echo ">> Cheking if files have been successfully downloaded"
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
        echo ">> $f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo ">> $f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..." 
        echo $f >> dlist.tmp
        mv  $MDIR/a/$f ../dumpster/${f}.$$
        #mv  $MDIR/$f ../dumpster/${f}.$$
      fi
    else
      echo ">> File $f has never been downloaded"
      echo  $f >> dlist.tmp
    fi
  done       # closes loop to check if file needs to be downloaded


  # get missing a-files for this month:
  echo ">> Getting missing a-files for $MDIR"
  echo ">> There are `grep $STATION dlist.tmp | wc -l | awk '{print $1}'` 15 sec files to download from the receiver for month $MDIR"
  DLIST=`cat dlist.tmp`
  for d in $DLIST
  do
    echo ">> Getting file $d"
    cd $LSDIR
   # wget -c ftp://${IPNRFTP}/$MDIR/a/$d
    curl -u ${USER}:${PWS} 'http://'${IPNR}':'${PORT}'/prog/download?loggedfile&path=/'$MDIR'/a/'$d'' > $MDIR/a/$d
  done            # closes file loop
#done            # closes month loop

if [ $B_FILES -eq 1 ]
then
######## get b-file list:  #########
  echo ">> Getting b-file list"
  curl -u ${USER}:${PWS} 'http://'${IPNR}':'${PORT}'/prog/show?loggedfiles&directory=/'${MDIR}'/b'  2>/dev/null | grep name | awk '{print substr($2,6,22), substr($3,6,8)}' | grep ${STATION} > tmplist1.tmp

  # check if more than one file NEED TO ADD IF LOOP
  echo "" > dlist1.tmp              # clear tmp list

  # check if files have already been successfully downloaded and download new if necessary:
  echo ">> Cheking if files have been successfully downloaded"
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
        echo ">> $f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo ">> $f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..." 
        echo $f >> dlist1.tmp
        mv  $MDIR/b/$f ../dumpster/${f}.$$
      fi
    else
      echo ">> File $f has never been downloaded"
      echo  $f >> dlist1.tmp
    fi
  done       # closes loop to check if file needs to be downloaded


  # get missing b-files for this month:
  echo ">> Getting missing b-files for $MDIR"
  echo ">> There are `grep $STATION dlist1.tmp | wc -l | awk '{print $1}'` 15 sec files to download from the receiver for month $MDIR"
  DLIST=`cat dlist1.tmp`
  for d in $DLIST
  do
    echo ">> Getting file $d"
    cd $LSDIR
    # wget -c ftp://${IPNRFTP}/$MDIR/b/$d
    curl -u ${USER}:${PWS} 'http://'${IPNR}':'${PORT}'/prog/download?loggedfile&path=/'$MDIR'/b/'$d'' > $MDIR/b/$d
  done            # closes file loop
fi
done            # closes month loop

cd $LSDIR
rm tmplist.tmp dlist.tmp tmplist1.tmp dlist1.tmp
# mirroring done!
echo ">> Mirroring complete!"


# transform the "latestish" files to rinex and move to correct places...
# First build a file list of existing 24hr T00 files i.e. dump new files into tmp directory:
echo ""
echo "## Transforming files to RINEX ##"
echo ""

cd $LSDIR
echo ">> Building a list of current files to compare to finished files..."

ls */a/*a.T00 > currlist.lst
ls */b/*b.T00 > currlist1.lst

echo ">> Picking out the a-files"

for f in `cat currlist.lst`
do
# see if files have already been transformed to rinex
  DFLAG=`grep $f donelist.lst | wc -l`
  if [ $DFLAG -eq 0 ]
  then
     echo ">> A-files that will be transformed: " $f
     # file has not been transformed
     cp $f $TMPDIR/.
     echo $f >> donelist.lst
   fi
  done

echo ">> Picking out the b-files"

for f in `cat currlist1.lst`
do
# see if files have already been transformed to rinex
  DFLAG=`grep $f donelist1.lst | wc -l`
  if [ $DFLAG -eq 0 ]
  then
     echo ">> B-files that will be transformed: " $f
     # file has not been transformed
     cp $f $TMPDIR1/.
     echo $f >> donelist1.lst
   fi
  done

# prepare and put 30 sec data for ukmet to the right place
cd /data/${SSS}/
echo " preparing 30 sec data for ukmet `date`..."
ls 20*/b/*T00 > currlist_met.lst
for f in `cat currlist_met.lst`
do
  # see if files have already been transformed to rinex
  DFLAG=`grep $f donelist_met.lst | wc -l`
  if [ $DFLAG -eq 0 ]
  then
    # file has not been transformed
    cp $f tmpmet/
    echo $f >> donelist_met.lst
  fi
done
echo "tmpmet"
cd tmpmet/
pwd
/home/gpsops/bin/mall_netrs_t00_met.sh *.T00
echo " done preparing 30 sec data for ukmet `date`..."

#### Then just whip all the new a-files to rinex! ####

cd $TMPDIR
T00FILES=`ls * | grep ${STATION}20 -c`
if [ $T00FILES -ne 0 ]
then
   echo ">> Starting the mall-script..."
   echo " "
   /home/gpsops/bin/mall_netrs_t00.sh *a.T00
else
   echo ">> No files to transform.."
fi

#### Finally whip all the new b-files to rinex! ####

cd $TMPDIR1
T00FILES=`ls * | grep ${STATION}20 -c`
if [ $T00FILES -ne 0 ]
then
   echo ">> Starting the mall-script..."
   echo " "
   /home/gpsops/bin/mall_netrs_t00_1hz.sh *b.T00
else
   echo ">> No files to transform.."
fi



echo ">> $0 ending `date`..."
# that's it!
 
