#!/bin/bash

#declare -a months

YEARmonths=`timecalc -l "%Y/%b " |  tr [:upper:] [:lower:] | tr ' ' '\n'  | uniq`

N9="T02"
NS="T00"
for ymonth in ${YEARmonths}
do
    for statpath in `ls -d /home/gpsops/data/${ymonth}/????`
    do
        echo "Working on $statpath"
        cd ${statpath}/1Hz_1hr/raw/
        STAT=`echo ${statpath} | cut -c 28-31`
        stat=`echo ${STAT} |  tr [:upper:] [:lower:]`
        echo "------------- ${STAT}-${month} -------------------"
        tmp=`ls ${STAT}${YEAR}*b.T??`
        rtype=`echo ${tmp} | cut -c 19-21 | uniq`
        if [ -n "$rtype" ]; 
        then
            filelist=`timecalc --list "${STAT}%Y%m%d%H00b.${rtype} "`
        fi

        for file in `echo ${filelist}`
        do
            fileEpoch=`echo ${file} | cut -c 5-14`
            rintimeform=`timecalc -r -f "%Y%m%d%H" -d ${fileEpoch}`
            rfile="../rinex/${stat}${rintimeform}d.Z"
            if [  -a ${rfile} ];
            then

                actualsize=$(du -k "$rfile" | cut -f 1)
                if [ $actualsize -lt 19 ];
                then
                    ls -l $rfile
                    
                    if [ $rtype == $N9  ];
                    then
                        echo "-------- Trimble NetR9 receiver -------"
                        /home/gpsops/bin/mall_netr9_1hz_t02.sh ${file} 
                    elif [ ${rtype} == ${NS}  ];
                    then
                        echo "######## Trimble NetRS receiver #######"
                        /home/gpsops/bin/mall_netrs_1hz_t00.sh  ${file}
                    else
                        echo "######## Receiver not regicnised #######"
                    fi
                fi

            else
                if [ ! -d  "../rinex/" ];
                then
                     mkdir -p ../rinex
                fi

                if [ ${rtype} == $N9  ];
                then
                    echo "-------- Trimble NetR9 receiver -------"
                    /home/gpsops/bin/mall_netr9_1hz_t02.sh ${file} 
                elif [ ${rtype} == $NS  ];                  
                then
                    echo "######## Trimble NetRS receiver #######"
                    /home/gpsops/bin/mall_netrs_1hz_t00.sh  ${file}
                else
                    echo "######## Receiver not regicnised #######"
                fi
            fi
            
        
        done
        filelist=""
        

    done
done

