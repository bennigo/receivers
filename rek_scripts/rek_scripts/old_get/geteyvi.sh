#!/bin/sh
# Hourly and daily download of Trimble NETRS receiver KVSK
# SFS jun 2011: Adapted from getsoho.sh to download from KVSK which now has 3G conection.
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

STATION=EYVI
STATION_FULL_NAME="EYvindstungur"
station=eyvi
sss=eyvi

IPNR=157.157.40.120     		# topcon ipnumber (for web interface use *:8080 otherwise you get the router)
IPNRFTP=157.157.40.120    		# topcon ipnumber
port=2121
PASSWORD=E67F8PV

LSDIR=/data/$STATION          #CREATE# 
LOGDIR=/home/gpsops/log/getdata/$STATION  #CREATE# 
TMPDIR=/data/$STATION/tmp	#CREATE#
TMPDIR1=/data/$STATION/tmp1   #CREATE#

TLOG1=$LOGDIR/hitiall.log		#TOUCH#
TLOG2=$LOGDIR/${station}.tp		#TOUCH#	
VLOG1=$LOGDIR/spennaall.log		#TOUCH#
VLOG2=$LOGDIR/${station}.sp		#TOUCH#

# Different global messages
MESSAGE=">> Modem"                 #ADAPT#
ABORT_MESSAGE="[Process aborted]"
OK_MESSAGE="[OK]"

# How many months to sync back
MONTHS=7
# Some date information
YEAR=`date +"%Y"`
MONTH=`date +"%m"`
DAY=`date +"%d"`

# Weclome message
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
    PNGCNT=`ping -c1 -w1 $IPNRFTP |grep transmit|awk '{print $4}'`

    if [ $PNGCNT -eq 0 ]
    then
       echo ">> Ping check #2: Does not answer ping"
       echo ">> Will try again"
       sleep 3
       PNGCNT=`ping -c1 -w1 $IPNRFTP |grep transmit|awk '{print $4}'`
       
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
####### mirror the receiver! #######

echo " "
echo "## Mirroring the receiver ##"
echo ""

cd ${LSDIR}
echo "Getting file list from the receiver for all files for the last $MONTHS months:"
#echo curl ftp://${IPNR} --user sil:${PASSWORD} | grep '[A-Z][A-Z][A-Z][A-Z].*'   2>/dev/null
FILELIST=`curl ftp://${IPNR}:${port} --user sil:${PASSWORD} | grep '[A-Z][A-Z][A-Z][A-Z].*'   2>/dev/null`
echo ">> Creating directory list from the file list"
# TOPCON VERSION

DIRLIST=`echo "${FILELIST}" | grep -o '[A-Z][A-Z][A-Z][A-Z].*' | awk '{print substr($1,5,2)}' | sort -r | uniq | tail -${MONTHS}`
echo "This is the DIRLIST:"
echo ${DIRLIST}
for MODIR in $DIRLIST
do
  MDIR=$YEAR${MODIR}
  cd ${LSDIR}
  echo ">> Working on month $MDIR"
  if [ -d $MDIR ]
  then
    :
  else
    #mkdir $MDIR; mkdir $MDIR/a; mkdir $MDIR/b
    mkdir $MDIR
    mkdir $MDIR/a
    mkdir $MDIR/b
  fi

######## get a-file list:  #########

  echo ">> Getting file list for the current month"
  echo "${FILELIST}" | grep ${STATION}${MODIR} > tmplist.tmp

  # check if more than one file NEED TO ADD IF LOOP
  echo "" > dlist.tmp              # clear tmp list
  
  # check if files have already been successfully downloaded and download new if necessary:
  echo ">> Cheking if files have been successfully downloaded"
  FLIST=`cat tmplist.tmp | awk '{print $9}'`
  for f in $FLIST
  do 
    FSISREM=`grep $f tmplist.tmp| awk '{print $5}'`
    if [ -f $MDIR/a/$f ]
    then
      # check size
      FSISLOC=`ls -l $MDIR/a/$f | awk '{print $5}'`
      if [ "$FSISREM" -eq "$FSISLOC" ]
      then
        echo ">> $f has already been successfully downloaded" > /dev/null
        # continue
      else
        echo ">> $f has already been downloaded but some bytes seem to be missing ($FSISLOC of $FSISREM downloaded), will try again..." 
        echo $f >> dlist.tmp
        mv  $MDIR/a/$f ../dumpster/${f}.$$
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
    cd $MDIR
    cd a
    wget -c --user=sil --password=${PASSWORD} ftp://${IPNRFTP}:${port}/$d
  done            # closes file loop
done            # closes month loop
cd $LSDIR

#rm tmplist.tmp dlist.tmp tmplist1.tmp dlist1.tmp
# mirroring done!
echo ">> Mirroring complete!"

#### RINEX ####

#echo "RINEX transform is disabled at the moment.."
# transform the "latestish" files to rinex and move to correct places...
# First build a file list of existing 24hr T00 files i.e. dump new files into tmp directory:
echo ""
echo "Transforming files to RINEX:"
cd $LSDIR
echo "--> Building a list of files..."
ls */a/*a > currlist.lst
#ls */b/*b.T02 > currlist1.lst

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
/home/gpsops/bin/mall_topcon_15s.sh ${STATION}* 


# transform the "latestish" files to rinex and move to correct places...
# First build a file list of existing 24hr T00 files i.e. dump new files into tmp directory:
echo ""
echo "## Transforming files to RINEX ##"
echo ""

cd $LSDIR
echo ">> Building a list of current files to compare to finished files..."

echo "-- > $0 ending `date`..."

