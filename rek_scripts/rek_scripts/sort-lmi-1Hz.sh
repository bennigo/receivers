#!/bin/bash
#   The scripts checks for rinex files in  /home/gpsops/tmp/RT-rinex/???? which are 
#   being converted from RT data streams to rinex format by bnc see processes 
#   bnc --conf /home/gpsops/.config/BKG/rtcm2rinex-????.bnc --nw
#   Currently the data from the following stations are being created through this process are
#   AKUR, GUSK, HEID, ISAF, MYVA, RHOL, SKHA, THOB, GRIC, ELDC, SVIN, GFUM
#
#   Created by bgo "bgo@vedur.is": Jan 2017


thjap="/home/gpsops/bin/thjap"
uthjap="/home/gpsops/bin/uthjap.py"
teqc="/home/gpsops/bin/teqcc"
prepath="/home/gpsops/data/"

ftype1s="1Hz_1hr/rinex/"
ftype15s="15s_24hr/rinex/"
ftype30s="30s_1hr/rinex/"

sampl30s="-O.int 30 -O.dec 30"
sampl15s="-O.int 15 -O.dec 15"


RT_path="/home/gpsops/tmp/RT-rinex/"

if [ $# -eq 0 ]
then
    echo "No arguments supplied"
    stat_list=`ls -d ${RT_path}????`
else
    stat_list=${RT_path}$1
fi


for statpath in ${stat_list}
do

    cd ${statpath}
    pwd
    STAT=`echo ${statpath} | cut -c 27-31`
    stat=`echo ${STAT} |  tr [:upper:] [:lower:]`
    echo "------------- ${STAT} -------------------"
    currhour=`timecalc -r H |  tr [:lower:] [:upper:]`
    ls *.??O  > /dev/null 2> /dev/null
    lsout=$? # will be zero if ls command before returned and existing file
    
    if [ ${lsout} == 0 ];
    then
        for file in `ls *.??O`
        do
            if [ "${STAT}${currhour}O" == ${file} ];
            then
                echo "${STAT}${currhour}O"
            else
                doy=`echo "$file" | cut -c 5-7`
                yr=`echo "$file" | cut -c 10-11`
                ymonth=`timecalc -f "%j%y" -o "%Y/%b" -d ${doy}${yr} |  tr [:upper:] [:lower:]`
                stdir1s="${prepath}${ymonth}/${STAT}/${ftype1s}"
                stdir30s="${prepath}${ymonth}/${STAT}/${ftype30s}"

                filepr=`echo ${file} | cut -c 1-11`
                Dfile="${filepr}D"
                dfile=`echo ${Dfile} | tr [:upper:] [:lower:]`
                Dfile="${Dfile}.Z"
                dfile="${dfile}.Z"


                if [ ! -d ${stdir1s} ];
                then
                   mkdir -p ${stdir1s}
                fi

                if [ ! -d ${stdir30s} ];
                then
                   mkdir -p ${stdir30s}
                fi

                # converting to hantaqa and moving the file to the right place
                ${thjap} $file
                cp ${Dfile} ${stdir1s}
                ffilev=${stdir1s}${Dfile}
                echo "$ffilev"

                if [ -f ${stdir1s}${Dfile} ];
                then
                    echo "File ${Dfile} has been copied to archive ${stdir1s}"
                    ${uthjap} ${Dfile}
                    ${teqc} ${sampl30s} $file > tmp
                    mv tmp $file
                    ${thjap} $file
                    mv $Dfile  ${stdir30s}$dfile

                    if [ -f ${stdir30s}${dfile} ];
                    then
                        echo "File ${dfile} has been moved to archive ${stdir30s}"
                    fi

                else
                    echo "There was a problem moving ${Dfile} to ${stdir1s}"
                fi
                
            fi
        done
    else
        echo "ls returned ${lsout}, no rinex files in directory"
    fi
    
done
