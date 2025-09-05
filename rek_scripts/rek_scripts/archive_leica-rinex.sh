#!/bin/bash
# bgo nov 2016: rinexing leica.
######################

# Some variables and paths


flist=$@


freq15sd="15s_24hr"
freq1hzd="1Hz_1hr"
rinexdir="rinex"
rawdir="raw"
pdir="/data"
re='^[0-9]+$'

for f in ${flist}; do
    stat=`echo $f | cut -c 1-4`
    doy=`echo $f | cut -c 5-7`
    freq=`echo $f | cut -c 8`
    yy=`echo $f | cut -c 10-11`
    ending=`echo $f | cut -c 12-`

    if  [[ $freq =~ $re ]] ; then
        freqd=${freq15sd}
    else
        freqd=${freq1hzd}
    fi

    rdir=`timecalc -l "${pdir}/%Y/bbb/${stat}/${freqd}/${rinexdir}/ " 1D -f %y-%j -d "$yy-$doy"`
    if [ ! -d ${rdir} ]; then
        echo "Directory ${rdir}  does not exist, creating ..."
        mkdir -p ${rdir}
    fi
    mv $f $rdir

    if [ -f "${rdir}${f}" ]; then
        echo "$f wash succsessfully moved to $rdir$f"
    fi

    
done
