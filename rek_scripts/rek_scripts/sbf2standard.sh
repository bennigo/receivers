# this script renames septentrio file format with day-of-year number format to our default format described by Trimble.

for f in $(ls | grep -v res)
 do
     STATION=$(echo $f | cut -c2-5)
     DAYNUMBER=$(echo $f | cut -c10-12)
     YEAR=$(echo $f | cut -c7-8)
 
     YMD=$(timecalc -d20"$YEAR"-"$DAYNUMBER" -f%Y-%j -t)
     FULLYEAR=$(echo $YMD | cut -f1 -d ' ')
     MONTH=$(echo $YMD | cut -f2 -d ' ')
     DAY=$(echo $YMD | cut -f3 -d ' ')
 
     echo "Input:"
     echo $STATION, $DAYNUMBER, $YEAR
     echo "Output:"
     echo $FULLYEAR$MONTH$DAY
 
     #mv $f $STATION$FULLYEAR$MONTH$DAY"0000a.sbf"
 
 done

