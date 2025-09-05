#!/bin/bash

#for i in $(ls /data/2016/mar/);
#do 
#    rsync -uva gpsops@rek.vedur.is:/data/2016/mar/${i}/1Hz_1hr/raw/* /data/2016/mar/${i}/1Hz_1hr/raw/.
#done


dirlist=`timecalc -l "/data/%Y/#b/ " 1D |  tr ' ' '\n'  | uniq`


for dir in ${dirlist}; 
do
    for i in $( ls -d ${dir}* );
    do
    
        #if [ ! -d ${i}/1Hz_1hr/raw ]; 
        #then
        #    echo "Directory ${i}/1Hz_1hr/raw  does not exist, creating ..."
           # mkdir -p ${i}/1Hz_1hr/raw
           # mkdir -p ${i}/1Hz_1hr/rinex
        #fi
        
        if [ ! -d ${i}/30s_1hr/rinex ]; 
        then
           echo "Directory ${i}/30s_1hr/rinex  does not exist"
           # mkdir -p ${i}/1Hz_1hr/rinex
        else
            echo "syncing  rek.vedur.is:/$i/30s_1hr/raw"
            rsync -uva /${i}/30s_1hr/rinex/* gpsops@sarpur.vedur.is:/srv/www/brunnur/gps/1hr/ 

        fi
        #rsync -uva gpsops@rek.vedur.is:${i}/1Hz_1hr/raw/* /${i}/1Hz_1hr/raw/.
        #rsync -uva /${i}/1Hz_1hr/rinex/* gpsops@sarpur.vedur.is:sarpur:/srv/www/brunnur/gps/1hr/ 
    done
done



# Rinexing...
#/home/gpsops/bin/runall_mall.sh
