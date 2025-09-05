#!/bin/bash

timetag=`date +%Y%m`
yeartag=`date +%Y`
system="seydisfjordur"
datadir="/home/gpsops/extension_meter/${yeartag}/${system}/"
script="/home/gpsops/bin/togmaeling.py"

if [ ! -d ${datadir} ];
then
    echo "Directory ${datadir}  does not exist, creating ..."
    mkdir -p ${datadir}
fi

archiveserver="gpsops@rawdata"

# aquisition from the lower station collocated with SVIN name sha1, ...
channels="1"

server=10.4.2.194
port=6785
timeout=3
name="se9"


for channel in ${channels}
do
   datafile="${system}_${name}"
   ${script} --server ${server} --port ${port} -ch ${channel} --timeout ${timeout} --save ${datadir}${datafile} --path ${datadir} --raw >> ${datadir}${datafile}${channel}-${timetag}
done

channels="1"

server=10.4.1.49
port=6785
name="sey"

for channel in ${channels}
do
   datafile="${system}_${name}"
   ${script} --server ${server} --port ${port} -ch ${channel} --timeout ${timeout} --save ${datadir}${datafile} --path ${datadir} --raw >> ${datadir}${datafile}${channel}-${timetag}
done
