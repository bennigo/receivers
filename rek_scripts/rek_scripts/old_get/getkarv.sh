#!/bin/sh
# get data from karv (ask) using rsync 
# Special karv: prepare 30 sec files for ukmet
# HG dec 2009: Use dump instead of brot
# HG jan. 2010: also get voltage and temp
# HG feb. 2010: reprogram to not to leave any files on kjarni
##################
SSS=KARV
sss=karv
cptr=ask
IP=10.140.48.21

echo "get${sss}.sh on rek2 starting `date`."
echo "This script grabs ${SSS} data existing on computer ${cptr}. You must login to ${cptr} if there is a problem with ${cptr}-${SSS} data transfer"

# Check if I am already running
ps -ef > /tmp/tmppps.$$
MENUM=`grep get${sss}.sh /tmp/tmppps.$$ | wc -l`
if [ $MENUM -gt 1 ]
then
  echo "A copy of get${sss}.sh is already running on rek2, exiting..."
  grep get${sss}.sh /tmp/tmppps.$$; rm /tmp/tmppps.$$; exit
fi
rm /tmp/tmppps.$$

# Check connection to kjarni and ask:
PNGCNT=`ping -c1 -w1 kjarni |grep transmit|awk '{print $4}'`
if [ $PNGCNT -eq 0 ]
then
  echo "kjarni.vedur.is does not answer ping, exiting...."; exit
else
  PNGCNT=`ssh -t kjarni "ping -c1 -w1 ${cptr}" 2>/dev/null | grep transmit| awk '{print $4}'`
  if [ $PNGCNT -eq 0 ]
  then
    echo "${cptr} does not answer ping, exiting..."; exit
  fi
fi

echo " comparing file listings on rek2 and ${cptr}..."

# compare directories on ask and rek2 and make new directories on rek2 when necessarey ( once per month)
cd /data/$SSS
ls > locdir.lst   # local dirctory structure
#ssh -t kjarni "ssh -t $cptr \" ls /usr/sil/gps/data/${IP} | cat \" 2>/dev/null " >remdir.lst 2>/dev/null   # remote dirctory structure # does not work for some reason...
ssh -t kjarni "ssh -t $cptr \" cd /usr/sil/gps/data/${IP}; ls | grep -v remdir > remdir.lst \" 2>/dev/null "  2>/dev/null   # remote dirctory structure 
ssh -t kjarni "rsync -uva -e ssh --timeout=600 --size-only ${cptr}:/usr/sil/gps/data/${IP}/remdir.lst /usr/sil/gps/Data/${SSS}/" > /dev/null 2>&1
rsync -uva -remove-sent-files -e ssh kjarni:/usr/sil/gps/Data/${SSS}/remdir.lst . > /dev/null 2>&1
for d in `cat remdir.lst`
do
  LNUM=`grep $d locdir.lst | wc -l`
  if [ $LNUM -eq 0 ]
  then
    echo "making folder $d on rek2"
    mkdir $d; mkdir ${d}/a;  mkdir ${d}/b
  fi
done
rm -rf locdir.lst remdir.lst
# compare files on rek2 and $cptr, build a "transfer file list"
rm -rf getfil.lst remfil.lst; touch getfil.lst    # be sure that list is not in use
ls */*/*.T00 > locfil.lst    # local file list
# ssh -t kjarni "ssh -t $cptr \" cd /usr/sil/gps/data/${IP}; ls */*/*.T00 | cat \" 2>/dev/null " >remfil.lst 2>/dev/null   # remote file list # does not work for some reason And this silly way of first creating the file on ask has to be used!
ssh -t kjarni "ssh -t $cptr \" cd /usr/sil/gps/data/${IP}; ls */*/*.T00 > remfil.lst \" 2>/dev/null "  2>/dev/null   # remote file list 
ssh -t kjarni "rsync -uva -e ssh --timeout=600 --size-only ${cptr}:/usr/sil/gps/data/${IP}/remfil.lst /usr/sil/gps/Data/${SSS}/" > /dev/null 2>&1
rsync -uva -remove-sent-files -e ssh kjarni:/usr/sil/gps/Data/${SSS}/remfil.lst . > /dev/null 2>&1
ssh -t kjarni "ssh -t $cptr \" rm /usr/sil/gps/data/${IP}/rem*.lst \"" >/dev/null 2>&1
ssh -t kjarni "rm /usr/sil/gps/Data/${SSS}/rem*.lst" > /dev/null 2>&1   # option remove sent files not working...
for f in `cat remfil.lst`
do
  if [ -f /data/${SSS}/${f} ]
  then
    continue
  else
    echo $f >> getfil.lst
  fi
done
rm -rf locfil.lst remfil.lst

GNUM=`cat getfil.lst | wc -l`
if [ $GNUM = 0 ]
then
  echo " No files to transfer, exiting..."; rm getfil.lst ; exit
fi

# get all missing files - be sure that size is the same in the end, otherwise delete the local file just downloaded
for f in `cat getfil.lst`
do
  echo " Transferring ${f}..."
  g=`echo $f | cut  -c10-30`
  # move file to kjarni:
  ssh -t kjarni "rsync -uva -e ssh --timeout=600 --size-only ${cptr}:/usr/sil/gps/data/${IP}/${f} /usr/sil/gps/Data/${SSS}/" > /dev/null 2>&1
  # move file from kjarni to rek2:
  rsync -uva kjarni:/usr/sil/gps/Data/${SSS}/${g} /data/${SSS}/${f} > /dev/null 2>&1
  ssh -t kjarni "rm /usr/sil/gps/Data/${SSS}/${g}" > /dev/null 2>&1
  # Verify that file is the same on rek2 as on $cptr
  LOCFILSIS=`ls -l /data/${SSS}/${f} | awk '{print $5}'`
  REMFILSIS=`ssh -t kjarni "ssh -t $cptr \" ls -l /usr/sil/gps/data/${IP}/${f} \" 2>/dev/null " 2>/dev/null | awk '{print $5}'`
  if [ "$LOCFILSIS" != "$REMFILSIS" ]
  then
    echo " transfer of file ${f} unsuccessful ($LOCFILSIS bytes on rek2 versus $REMFILSIS bytes on $cptr), will remove it from rek2 to allow for a later try"
    rm /data/${SSS}/${f}
  fi
done
rm getfil.lst

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
cd tmpmet
/home/gpsops/bin/mall_netrs_t00_met2.sh *.T00
echo " done preparing 30 sec data for ukmet `date`..."

# make new rinex files and clean up
cd /data/${SSS}/
ls 20*/a/*T00 > currlist.lst
echo " preparing to make rinex files..."
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
echo " converting a.T00 files to rinex..."
cd tmp
/home/gpsops/bin/mall_netrs_t00.sh *.T00

echo " getting voltage and temp data... "
########### Get logfiles from ${cptr}:/usr/sil/gps/Logs/ ##################
cd /home/gpsops/log/getdata/$SSS

# rename on remote to prevent overwrite:
ssh -t kjarni "ssh -t $cptr \"mv /usr/sil/gps/Logs/${sss}.sp /usr/sil/gps/Logs/${sss}tmp.sp\"" > /dev/null 2>&1
ssh -t kjarni "ssh -t $cptr \"mv /usr/sil/gps/Logs/${sss}.tp /usr/sil/gps/Logs/${sss}tmp.tp\"" > /dev/null 2>&1

# get files to kjarni and remove remote files
ssh -t kjarni "rsync -uva -remove-sent-files -e ssh --timeout=600 --size-only ${cptr}:/usr/sil/gps/Logs/${sss}tmp.sp /usr/sil/gps/tmplogs/" > /dev/null 2>&1
ssh -t kjarni "rsync -uva -remove-sent-files -e ssh --timeout=600 --size-only ${cptr}:/usr/sil/gps/Logs/${sss}tmp.tp /usr/sil/gps/tmplogs/" > /dev/null 2>&1

# get files to rek2 and remove from kjarni
rsync -uva --remove-sent-files -e ssh --timeout=600 --progress --size-only kjarni:/usr/sil/gps/tmplogs/${sss}tmp.sp . > /dev/null 2>&1
rsync -uva --remove-sent-files -e ssh --timeout=600 --progress --size-only kjarni:/usr/sil/gps/tmplogs/${sss}tmp.tp . > /dev/null 2>&1
cat ${sss}tmp.sp >> ${sss}.sp
cat ${sss}tmp.tp >> ${sss}.tp
# put to dump as well:
#scp ${sss}tmp.sp sil@frumgogn:/home/sil/gps_01/logs/voltage_temp/ > /dev/null 2>&1
#scp ${sss}tmp.tp sil@frumgogn:/home/sil/gps_01/logs/voltage_temp/ > /dev/null 2>&1
#ssh dump "cd /home/sil/gps_01/logs/voltage_temp/; cat ${sss}tmp.tp >> ${sss}.tp; cat ${sss}tmp.sp >> ${sss}.sp; rm ${sss}tmp.tp ${sss}tmp.sp"
rm ${sss}tmp.sp ${sss}tmp.tp

echo "          $0 on rek2 ending `date`..."
# that's it!

