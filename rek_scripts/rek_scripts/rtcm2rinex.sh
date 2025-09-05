#!/bin/bash
#   The script runs 
#   nohup bnc --conf /home/gpsops/.config/BKG/rtcm2rinex-????.bnc --nw &
#   which converts rtcm2 RT streams to rinex hourly files
#   (The run is defined in the config file /home/gpsops/.config/BKG/rtcm2rinex-????.bnc)
#   where ???? is a four letter station name
#   The stations with station files in /home/gpsops/.config/BKG/rtcm2rinex-????.bnc are
#   compaired with running processes and if the bnc is not running the for station defined
#   in the config directory bnc will be executed for that file
#
#   if killing all bnc prcesses is necisary 
#   killall bnc
#
#   Created by bgo "bgo@vedur.is": feb 2017, edited nov, 2023

BNC="/home/gpsops/bin/bnc"
confdir="/home/gpsops/.config/BKG/"

STALIST=`ls ${confdir}rtcm2rinex-* | grep -oP 'rtcm2rinex-\K[0-9A-Z].+(?=\.)' | sort | uniq`
PROCLIST=`ps -aux | grep -oP 'rtcm2rinex-\K[0-9A-Z].+(?=\.)' | sort | uniq`
read -a stalist -d ' ' <<< "$STALIST" 
read -a proclist -d ' ' <<< "$PROCLIST" 

# echo ${stalist[@]}  
# echo ${proclist[@]}
stalist+=("${proclist[@]}")
stalist=`echo "${stalist[@]}" | tr ' ' '\n' | sort | uniq -u`

if [ -z "$stalist" ]
then
    echo "BNC running for all stations"
else
    echo "BNC not running for stations: $stalist"
fi

for sta in $stalist
do
    echo "Starting rtcm2rinex conversion for $sta"
    nohup $BNC --conf ${confdir}rtcm2rinex-${sta}.bnc -nw 2> /dev/null &
done
