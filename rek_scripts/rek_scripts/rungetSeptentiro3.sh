#!/bin/bash
while :
do
    echo "Press [CTRL+C] to stop.."
    python ~/bin/getSeptentrio3 -s 20211115-0000 -e 20211117-0000 -se 15s_24hr -comp .gz -sy JONC
    #python ~/bin/getSeptentrio3 -D 20 -se 15s_24hr -comp .gz -sy JONC
    sleep 1
done
