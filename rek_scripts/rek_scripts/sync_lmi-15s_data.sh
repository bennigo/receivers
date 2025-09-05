#!/bin/bash
# Download 15s GPS data from lmi ftp server. 
# adapted for gnsmart server
# BGO Oct 2016
##################################


if [ $# == 0 ];
then
    stationlist="AKUR HEID ISAF GUSK MYVA"
    twindow=40
else
    twindow=`expr $1 + 1`; shift
    stationlist=$@
fi


lmifreq="/15s_data"
freqd="15s_24hr"
format="rinex"
pdir="/data"
#ftpserver="ftp://213.167.147.213/gnsmart_data" Old server name
ftpserver="ftp://ftp.lmi.is/.gnsmart_data"
dumpserver="sil@frumgogn.vedur.is:/home/sil/gps_01/rinex"

today=`timecalc -o %Y%j`


for stat in ${stationlist};
do
    
    
    filelist=`timecalc -l "${pdir}/%Y/#b/${stat}/${freqd}/${format}/${stat}#Rin2D.Z " 1D -D ${twindow}` 
    for f in ${filelist};
    do
        echo "File $f -------------------------------------------------"
        # the date
        year=`echo $f | cut -c 7-10`
        doy=`echo $f | cut -c 40-42`
        day=${year}${doy}

        rdir=`timecalc -l -f %Y%j -d ${day} "${pdir}/%Y/#b/${stat}/${freqd}/${format}/ " 1D`

        if [ ! -d ${rdir} ]; 
        then
            echo "Directory ${rdir}  does not exist, creating ..."
            mkdir -p ${rdir}
        fi

        lrfile=`timecalc -f %Y%j -d ${day} -l "${pdir}/%Y/#b/${stat}/${freqd}/${format}/${stat}#Rin2D.Z " 1D` 
        dumpdir=`timecalc -f %Y%j -d ${day} -l "${dumpserver}/%Y/${stat}/ " 1D` 
        rrfile=`timecalc -f %Y%j -d ${day} -l "${ftpserver}${lmifreq}/%Y/#gpsw/%j/${stat}#Rin2e " 1D`
        echo "$lrfile"

        if [ $day == ${today} ]; # if day is today still try to download
        then
            echo "special case today, not implemented "
            :
        else 
            echo "Check if ${lrfile} exists ..."
            if [ ! -f $f ]; # checking if file does not exists and then download
            then
                echo "${rrfile} does not exist will try to download and sync to frumgogn ..."
                wget -N -c -O ${lrfile} ${rrfile}
                rsync -uva --timeout=30 ${lrfile} ${dumpdir}
           else 
                echo "... $lrfile exists, comparing the size with the remote file"
                echo "..."
                rfsize=`wget --spider ${rrfile} 2>&1 | grep SIZE | gawk '{print $5}'`
                lfsize=`wc -c $f  | gawk '{print $1}'`
                diffr=`expr ${rfsize} - ${lfsize}`

                if [ ${rfsize} == "done."  ];
                then 
                    echo "${rrfile} no longer awailable on server" 
                else
                    if  [ ${diffr} -gt 0 ];
                    then # Re-downloading if local file is smaller then remote file
                        echo "WARNING: $f smaller then ${rrfile}."
                        echo "The difference is ${diffr} bytes re-downloading ..."
                        wget -N -c -O ${lrfile} ${rrfile}
                        rsync --timeout=30 ${lrfile} ${dumpdir}
                    else
                        echo "The files are the same the difference is ${diffr} bytes, doing nothing ..."
                    fi
                fi
            fi
        fi
        echo "Done with $f ============================================"
        echo "."
        echo "."
        echo "."
    done
done



# Rinexing...
#/home/gpsops/bin/runall_mall.sh
