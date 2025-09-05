#!/bin/sh
# get GPS data from ISGPS station GOLA	 
# Download god:/usr/sil/gps/data/pickup/ to rek2:/data/GOLA/ and clean up
# Call download program on eldvarp to get the data from hau to eldvarp
# adapded to run through eldvarp
# dori 2008, based on getskro.sh
# Added getting voltage and temp oct 2009 directly instead of running script from jar [HG]
# HG dec 2009: Use dump instead of brot
# HG mar 2010: put 1hr 1 sec data on dump
# MJR mar 2010: edited for use with BAS2
#####################
SSS=BAS2
sss=bas2
cpr=bas
DUMP1hr=/home/sil/gps_01/1hr_rinex/
echo ""
echo "*********** get${sss}.sh on rek2 starting at `date` ***********"

# Check if I am already running
ps -ef > /tmp/tmppps.$$
MENUM=`grep getbas2.sh /tmp/tmppps.$$ | wc -l`
if [ $MENUM -gt 1 ]
then
  echo "A copy of getbas2.sh is already running, exiting..."
  grep getbas2.sh /tmp/tmppps.$$; rm /tmp/tmppps.$$; exit
fi
rm /tmp/tmppps.$$

########### Get data from kjarni:/usr/sil/gps/Data/BAS2/ and make rinex files ##################
echo "getting data from kjarni to rek2 using rsync..."
cd /data/${SSS}
rsync -uva kjarni:/usr/sil/gps/Data/${SSS}/??????/a/* . #Collect 'a' data pool
rsync -uva kjarni:/usr/sil/gps/Data/${SSS}/??????/??/b/* . #Collect 'b' data pool
rsync -uva kjarni:/usr/sil/gps/Data/${SSS}/??????/h/* . #Collect 'h' data pool

mv *b.T00 /data/${SSS}/1hz #Move 1 Hz data to 1 Hz directory
mv *h.T00 /data/${SSS}/5hz #Move 5 Hz data to 5 Hz directory, if available
rm -f *.T00.A #Remove 'active' files

echo "transform new files to rinex..."
ls *a.T00 > currlist.lst
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
cd tmp
/home/gpsops/bin/mall_netrs_t00.sh *.T00

################################# and now the 1hz data
cd /data/${SSS}/1hz
ls *b.T00 > currlist.lst
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
cd tmp
/home/gpsops/bin/mall_netrs_t00_1hz.sh *b.T00

############################################
echo " getting voltage and temperature log files from bas..."
cd /data/${SSS}/tmp
ssh -t kjarni "rsync -uva --timeout=600 --progress --size-only ${cpr}:/usr/sil/gps/Logs/${sss}.sp /usr/sil/gps/Data/${SSS}/" > /dev/null 2>&1
ssh -t kjarni "rsync -uva --timeout=600 --progress --size-only ${cpr}:/usr/sil/gps/Logs/${sss}.tp /usr/sil/gps/Data/${SSS}/" > /dev/null 2>&1
ssh -t kjarni "ssh -t ${cpr} \" rm /usr/sil/gps/Logs/${sss}.sp\" " > /dev/null 2>&1
ssh -t kjarni "ssh -t ${cpr} \" rm /usr/sil/gps/Logs/${sss}.tp\" " > /dev/null 2>&1
rsync -uva --timeout=600 kjarni:/usr/sil/gps/Data/${SSS}/${sss}.sp /data/${SSS}/tmp/ > /dev/null 2>&1
rsync -uva --timeout=600 kjarni:/usr/sil/gps/Data/${SSS}/${sss}.tp /data/${SSS}/tmp/ > /dev/null 2>&1
ssh -t kjarni "rm /usr/sil/gps/Data/${SSS}/${sss}.sp /usr/sil/gps/Data/${SSS}/${sss}.tp" > /dev/null 2>&1
cat /data/${SSS}/tmp/${sss}.sp >> all_${sss}.sp 
cat /data/${SSS}/tmp/${sss}.tp >> all_${sss}.tp 
#scp /data/${SSS}/tmp/${sss}.sp sil@frumgogn:/home/sil/gps_01/logs/voltage_temp/tmp${sss}.sp
#scp /data/${SSS}/tmp/${sss}.tp sil@frumgogn:/home/sil/gps_01/logs/voltage_temp/tmp${sss}.tp
#ssh dump "cd /home/sil/gps_01/logs/voltage_temp; cat tmp${sss}.sp >> ${sss}.sp; cat tmp${sss}.tp >> ${sss}.tp; rm tmp${sss}.sp tmp${sss}.tp"
rm /data/${SSS}/tmp/${sss}.tp /data/${SSS}/tmp/${sss}.sp 

# that's it!

echo "          $0 on rek2 ending `date`..."
