#!/bin/bash
# rsync rinex data from frumgogn.vedur.is to rek2. 
# BGO sep 2017
##################################


server="sil@frumgogn.vedur.is"
prpath="/home/sil/gps_01/"
ftype="rinex"
freqd="15s_24hr"

Dzend='D.Z'


for Y in $@;
do
    ypath=${prpath}${ftype}/${Y}
    fpath=`ssh ${server} ls -d ${ypath}/????/*`
    for path in $fpath;
    do
        #ftpath=`ssh ${server} ls ${path}`
        station=`echo $path | cut -c 29-32`
        file=`echo $path | cut -c 38-47`
        
        #constructing the file path based on the rinex date in the file name
        lpath=`timecalc -l "/data/%Y/bbb/${station}/${freqd}/${ftype}/"  1D -f %j0.%yD.Z -d $file`

        if [ ! -d ${lpath} ]; then
            echo "Directory ${lpath}  does not exist, creating ..."
            mkdir -p ${lpath}
        else
            echo "Directory ${lpath} exists, continue ..."
        fi

        if [ ! -f ${lpath}${station}${file} ]; then
            echo "File ${station}${file} does not exist, rsyncing ..."
            rsync -uva ${server}:$path $lpath
        else
            fsize=`wc -c ${lpath}${station}${file} | gawk '{print $1}'`
            #if [ $fsize -ge 700000 ]; then
            #    echo "File  ${lpath}${station}${file} exists and is ${fsize} bytes, moving on ..."
            #    continue 
            #else
            #    echo "File ${file} is only ${fsize} bytes re-syncing ..."
            #    rsync -uva ${server}:$path $lpath
            #fi
            echo "File  ${lpath}${station}${file} exists and is ${fsize} bytes, moving on ..."
        fi

    done
done


