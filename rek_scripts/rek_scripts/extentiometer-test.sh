#!/bin/bash

timetag=`date +%Y%m`
system="vedurstofa-test"
datadir="/home/gpsops/extension_meter/${system}/"

channels="1"


server=10.4.1.196
port=6786
timeout=3

for channel in ${channels}
do
    datafile="${system}-channel${channel}-${timetag}"
    /usr/local/bin/togmaeling.py --server ${server} --port ${port} -ch ${channel} --timeout ${timeout} --save ${system} --path ${datadir} --raw >> ${datadir}${datafile}
done
