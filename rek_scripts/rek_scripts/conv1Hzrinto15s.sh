#!/bin/bash
#   Script that checks if 15 s file are precent.
#   If they are not tryes to make them from 1 Hz data
#
#   Created by bgo "bgo@vedur.is": mar 2021


thjap="/home/gpsops/bin/thjap"
uthjap="/home/gpsops/bin/uthjap.py"
teqc="/home/gpsops/bin/teqcc"
prepath="/home/gpsops/data/"
scrachpath=`echo ~/tmp/scrach/`

if [ ! -d ${scrachpath} ]
then
    mkdir -p ${scrachpath}
fi


ftype1s="1Hz_1hr/rinex/"
ftype15s="15s_24hr/rinex/"
ftype30s="30s_1hr/rinex/"

sampl1s="-O.int 30 -O.dec 30"
sampl15s="-O.int 15 -O.dec 15"
yesterday=`timecalc -D 1 -o %Y-%m-%d`

RT_path="/home/gpsops/tmp/RT-rinex/"

if [ $# -eq 0 ]
then
    echo "No arguments supplied"
    D=30
    stat_list=`ls -d /data/????/???/???? | cut -c  16-19 | sort | uniq`
else
    D=$1
    stat_list=${@:2}
fi

dirlist=`timecalc -D ${D} -l "/data/%Y/#b/ " 1D |  tr ' ' '\n'  | uniq`

for STAT in ${stat_list}
do
    for month in `echo ${dirlist} | gawk '{do printf "%s"(NF>1?FS:RS),$NF;while(--NF)}'i`
    do
        stdir15s="${month}${STAT}/${ftype15s}"
        stdir1s="${month}${STAT}/${ftype1s}"
    
        if test -d ${stdir15s};
        then
            :
            #echo "Directory ${stdir15s} exists: Moving on ..."
        else
            echo "Creating ${stdir15s} ..."
            mkdir -p ${stdir15s}
        fi
        
        
        rinex_hant24hf=`timecalc -D ${D} -d ${yesterday} -l "/data/%Y/#b/${STAT}/${ftype15s}${STAT}#Rin2D.Z " 1D |  tr ' ' '\n'  | uniq`
        for file in ${rinex_hant24hf};
        do
            cmpfile=true
            if [ -f  ${file} ];
            then
                fsize=`stat -c %s ${file}`
                    if [ ${fsize} -gt 300000 ];
                then
                    echo "File ${file} exsist and is ${fsize}b: moving on ..."
                    year=`echo ${file} | cut -c 7-10`
                    rsync -uva ${file} sil@frumgogn:/home/sil/gps_01/rinex/${year}/${STAT}/
                    cmpfile=false
                fi
            fi
                   
                    
            if ${cmpfile};
            then
                cwd=`pwd`
                cd ${scrachpath}
                echo "${file} does not exist or is < 700000 b, Will try to create from 1Hz data"
                doy=`echo "${file}" | cut -c 40-42`
                yy=`echo "${file}" | cut -c 45-46`
                day=`timecalc -o "%Y-%m-%d %H" -f "%y-%j %H" -d "${yy}-${doy} 23"`

                hrpaths=`timecalc -D 1 -f "%Y-%m-%d %H" -d "${day}" -l "/data/%Y/#b/${STAT}/${ftype1s}${STAT}#RIN2D.Z " 1H`
                hrfiles=`timecalc -D 1 -f "%Y-%m-%d %H" -d "${day}" -l "${STAT}#RIN2D.Z " 1H`
                hrofiles=`timecalc -D 1 -f "%Y-%m-%d %H" -d "${day}" -l "${STAT}#RIN2O " 1H`
                drin2=`timecalc -f "%y-%j" -d "${yy}-${doy}" -r d`
                
                ropsfile="${STAT}${drin2}O"

                cp ${hrpaths} ${scrachpath}
                ${uthjap} ${hrfiles}
                ${teqc} ${sampl15s} ${hrofiles} > ${ropsfile}
                ${thjap} ${ropsfile}
                mv "${STAT}${drin2}D.Z" $file
                rm ${hrofiles}
            
                if [ -f  ${file} ];
                then
                    fsize=`stat -c %s ${file}`
                    echo "Sucsessfully created ${file} of ${fsize}b:"
                    echo "syncing to frumgogn:/home/sil/gps_01/"
                    year=`echo ${file} | cut -c 7-10`
                    rsync -uva ${file} sil@frumgogn:/home/sil/gps_01/rinex/${year}/${STAT}/
                fi
            fi
        done
    done
done 
