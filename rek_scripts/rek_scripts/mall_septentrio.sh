#!/bin/bash
# bgo nov 2016: rinexing septentrio.
######################

# Some variables and paths

if [ $# == 0 ];
then
    echo "usage: $0 days stations, Abording ..."
    exit 1
else
    twindow=`expr $1 + 1`; shift
    stationlist=$@
fi

SCRPATH=/home/gpsops/bin/
CONPATH=/home/gpsops/confiles/

freqd="15s_24hr"
rinexdir="rinex"
rawdir="raw"
rfend="sbf"
pdir="/data"
pdir2="/home/gpsops/archive"
dumpserver="sil@frumgogn.vedur.is:/home/sil/gps_01/rinex"
zipped=""
DZend="D.Z"


today=`timecalc -o %Y%m%d`



for stat in ${stationlist};
do
    
    st=`echo ${stat} | tr 'A-Z' 'a-z'`
    CONFIL="config-${st}"

    tmpdir="/home/gpsops/tmp/rinex/${stat}"
    if [ ! -d ${tmpdir} ]; 
    then
        echo "Directory ${tmpdir}  does not exist, creating ..."
        mkdir -p ${tmpdir}
    fi
    cd $tmpdir # temp dir while rinexing.

    #rinfilelist=`timecalc -l "${pdir}/%Y/#b/${stat}/${freqd}/${rinexdir}/${stat}RinexD.Z " 1D -D ${twindow}`
    rawfilelist=`timecalc -l "${pdir}/%Y/#b/${stat}/${freqd}/${rawdir}/${stat}%Y%m%d0000a.${rfend} " 1D -D ${twindow} | tr ' ' '\n'  | uniq | grep -v '^$' | grep -v ${today}`
    rawdirs=`timecalc -l "${pdir}/%Y/#b/${stat}/${freqd}/${rawdir}/ " 1D -D ${twindow} | tr ' ' '\n'  | uniq | grep -v '^$'`
    rinexdirs=`timecalc -l "${pdir}/%Y/#b/${stat}/${freqd}/${raw}/ " 1D -D ${twindow} | tr ' ' '\n'  | uniq | grep -v '^$'`
    

    for f in ${rawfilelist};
    do

        
        rawd=`echo $f | rev |  cut -c 22- | rev`
        tmp=`echo $rawd | cut -c 6- `

        if [ ! -d ${rawd} ]; 
        then
            echo "Directory ${rawd}  does not exist trying the arcivee"
            if [ -d ${pdir2}$tmp ]; then
                mkdir -p $rawd
                cp  ${pdir2}$tmp/* $rawd
            fi
        fi

        
        day=`echo $f | rev | cut -c 10-17 | rev`
        year=`echo $f | rev | cut -c 14-17 | rev`
        rinexf=`timecalc -l "${stat}#Rin2" 1D -f %Y%m%d -d ${day}`


        rinexd=`timecalc -l "${pdir}/%Y/#b/${stat}/15s_24hr/rinex/" 1D -f %Y%m%d -d ${day}`
        if [ ! -d ${rinexd} ]; 
        then
            echo "Directory ${rinexd}  does not exist, creating ..."
            mkdir -p ${rinexd}
        fi
        
        rawf=`echo $f | rev | cut -c 1-21 | rev`
        
        if [ -f ${rinexd}${rinexf}${DZend} ]; then
            fsize=`wc -c ${rinexd}${rinexf}${DZend} | gawk '{print $1}'`
            if [ $fsize -ge 1000000 ]; then
                echo "File  ${rinexd}${rinexf}${DZend} exists and is ${fsize} bytes, moving on ..."
                continue 
            fi
        fi

        if [ ! -f ${f}* ]; then
            echo "$f does not exists skipping ..."
            continue
        fi

        if [[ `ls ${f}*` == *".gz" ]]; then
            zipped=".gz" 
        fi

        if [ -f $f${zipped} ]; then
            cp $f${zipped} .
            if [ ! -z  $zipped  ]; then
                 gunzip -f ${rawf}$zipped
            fi
            
            ls $rawf
            echo $f
            sbf2rin -v -f ${rawf} -o ${rinexf}T
            cp ${rinexf}T tmp.file

            ${SCRPATH}/teqcc -n_GLONASS 30 +err err.lst -config ${CONPATH}${CONFIL}  ${rinexf}T > "${rinexf}O"
            ${SCRPATH}/thjap  "${rinexf}O"
            rsync -uva  "${rinexf}${DZend}" sil@frumgogn:/home/sil/gps_01/rinex/$year/${stat}/
            
            echo "Moving ${rinexf}${DZend} to  ${rinexd}"
            mv  "${rinexf}${DZend}" ${rinexd}
            rm ${rawf} ${rinexf}T
            rm *.??T
        fi
    done

done
