#!/bin/bash
# bgo nov 2016: rinexing leica.
######################

# Some variables and paths

if [ $# == 0 ];
then
    stationlist="SKFC"
    twindow=10
else
    twindow=`expr $1 + 1`; shift
    stationlist=$@
fi

SCRPATH=/home/gpsops/bin/
CONPATH=/home/gpsops/confiles/

freqd="15s_24hr"
rinexdir="rinex"
rawdir="raw"
rfend="tps"
pdir="/data"
pdir2="/home/gpsops/archive"
dumpserver="sil@frumgogn.vedur.is:/home/sil/gps_01/rinex"

today=`timecalc -o %Y%m%d`



for stat in ${stationlist};
do
    
     st=`echo ${stat} | tr [A-Z] [a-z]`
     CONFIL=config-${st}

    tmpdir="/home/gpsops/tmp/rinex/${stat}"
    if [ ! -d ${tmpdir} ]; 
    then
        echo "Directory ${tmpdir}  does not exist, creating ..."
        mkdir -p ${tmpdir}
    fi
    cd $tmpdir # temp dir while rinexing.

    rinfilelist=`timecalc -l "${pdir}/%Y/bbb/${stat}/${freqd}/${rinexdir}/${stat}RinexD.Z " 1D -D ${twindow}`
    rawfilelist=`timecalc -l "${pdir}/%Y/bbb/${stat}/${freqd}/${rawdir}/${stat}%Y%m%d0000a.${rfend} " 1D -D ${twindow} | tr ' ' '\n'  | uniq | grep -v '^$' | grep -v ${today}`
    rawdirs=`timecalc -l "${pdir}/%Y/bbb/${stat}/${freqd}/${rawdir}/ " 1D -D ${twindow} | tr ' ' '\n'  | uniq | grep -v '^$'`
    rinexdirs=`timecalc -l "${pdir}/%Y/bbb/${stat}/${freqd}/${raw}/ " 1D -D ${twindow} | tr ' ' '\n'  | uniq | grep -v '^$'`
    

    for f in ${rawfilelist};
    do
        
        rawd=`echo $f | rev |  cut -c 22- | rev`
        tmp=`echo $rawd | cut -c 6- `
        echo $tmp

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
        rinexf=`timecalc -l "${stat}Rinex" 1D -f %Y%m%d -d ${day}`


        rinexd=`timecalc -l "${pdir}/%Y/bbb/${stat}/15s_24hr/rinex/ " 1D -f %Y%m%d -d ${day}`
        if [ ! -d ${rinexd} ]; 
        then
            echo "Directory ${rinexd}  does not exist, creating ..."
            mkdir -p ${rinexd}
        fi
        
        echo $rinexf
        rawf=`echo $f | rev | cut -c 1-21 | rev`
        if [ -f $f ]; then
            cp $f .

            ${SCRPATH}/teqcc  +err err.lst -config ${CONPATH}${CONFIL}  $rawf > "${rinexf}O"
            ${SCRPATH}/thjap  "${rinexf}O"
            rsync -uva  "${rinexf}D.Z" sil@frumgogn:/home/sil/gps_01/rinex/$year/${stat}/
            mv  "${rinexf}D.Z" ${rinexd}
            echo "Moving ${rinexf}D.Z to  ${rinexd}"
            rm ${rawf}
        fi
    done

done
