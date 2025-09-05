#!/bin/bash

timetag=`date +%Y%m`
yeartag=`date +%Y`
system="svinafellsheidi"
datadir="/home/gpsops/extension_meter/${yeartag}/${system}/"


if [ ! -d ${datadir} ];
then
    echo "Directory ${datadir}  does not exist, creating ..."
    mkdir -p ${datadir}
fi

archiveserver="gpsops@rawdata"

# aquisition from the lower station collocated with SVIN name sha1, ...
channels="1"

#server=10.4.2.97
server=10.4.2.22
port=6785
timeout=3
name="sha"


#for channel in ${channels}
#do
#    datafile="${system}_${name}${channel}-${timetag}"
#    /usr/local/bin/togmaeling.py --server ${server} --port ${port} -ch ${channel} --timeout ${timeout} --save ${system}_${name} --path ${datadir} --raw >> ${datadir}${datafile}
#done

server2=10.4.2.22
port2=2166
timeout2=2500
st_id="sha"

/home/gpsops/bin/togmaeling3.py --server ${server2} --port ${port2} -t ${timeout2}  download -u  root -p root -s ${st_id}  collecting
echo "home/gpsops/bin/togmaeling3.py --server ${server2} --port ${port2} -t ${timeout2}  download -u  root -p root -s     ${st_id}  collecting"
#-------------
# Efri stöð
timeout2=2500
st_id="shg"
#cautus
server2=10.4.2.18
port2=21
#/home/gpsops/bin/togmaeling3.py --server ${server2} --port ${port2} -t ${timeout}  download -u  root -p root -s ${st_id}  collecting

#teltonica router
#server2=10.4.1.86
#port2=2166
#server2=10.4.1.52
server2=10.6.1.87
port2=2167

/home/gpsops/bin/togmaeling3.py --server ${server2} --port ${port2} -t ${timeout}  download -u  root -p root -s ${st_id}  collecting
rsync -uva /mnt/datadiskur/extension_meter/  gpsops@rawdata:~/extension_meters/


#channels="1 2"

#server=10.4.1.196
#port=6786

#for channel in ${channels}
#do
#    datafile="${system}-channel${channel}-${timetag}"
#    /usr/local/bin/togmaeling.py --server ${server} --port ${port} -ch ${channel} --timeout ${timeout} --save ${system} --path ${datadir} --raw >> ${datadir}${datafile}
#done
