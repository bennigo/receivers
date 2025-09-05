ps -fu gpsops
echo ""
echo "**** Data in house ***"
isgpslog | grep -v " 0 " | grep -v " 0.00 " -c
