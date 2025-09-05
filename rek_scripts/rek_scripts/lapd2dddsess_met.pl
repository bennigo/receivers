#!/usr/bin/perl -w
# skript til að æla út ddd sess yr út úr lapdogsnafngiftum
#     er keyrt.
# notkun: lapd2dddsess.pl YEAR month day hour
#   t.d. lapd2dddsess.pl 2002 02 08 00 gefur 039 0 02
#
# Apað að sumu leyti eftir skripti frá Uwe Hessels (BKG). dori, okt. 2000.
#############################################################################
use strict "subs";

# ext. variables:
($YEAR,$MONTH,$DAY,$HOUR)=@ARGV;
if ($HOUR > 23) {die "Illegal value of hour, you get nothing! \n";}

# calculations of date :
my ($sec,$min,$hour,$mday,$mon,$year,$wday,$yday,$isdist,$yy,$doy);
 
#
# function daynr($year,$month,$day)
#
sub daynr
{
my ($year,$month,$day);
my $daynr=0;
my $idx;
my @monatstage = (31,28,31,30,31,30,31,31,30,31,30,31);
($year,$month,$day) = @_;
 
if ( $year < 1900) { return(-1); }
if ( $month <1 || $month > 12 ) { return(-1); }
 
$idx=leap($year);
if ( $idx ==1 ) { $monatstage[1] = 29; } elsif ( $idx == -1) { return(-1); }
if ( $month > $monatstage[$month-1] ) { return(-1); }
 
for ( $idx=1; $idx<$month; $idx++) { $daynr += $monatstage[$idx-1]; }
$daynr += $day;
 
return $daynr;
}

#
# function leap($year)
#  ATH: Klikkar næst árið 2100 (það er EKKI hlaupaár, en 4 gengur samt upp í það)
#sub leap($year) {
sub leap
{
 my $year;
 
($year)= @_;
 
if ( $year < 1900 ) {
   return(-1);
   }
 
if ( ($year %4 == 0) || ( ($year % 100 == 0) && ($year % 400 != 0) ) ) {
   return(1);
   } else {
   return(0);
   }
 
}
# end of sub leap

# calculate session number (æji helv. er þetta ljótt! Er ekki til betri aðferð?)
my $SESS=0;
if ($HOUR==00) { $SESS="a";}
if ($HOUR==01) { $SESS="b";}
if ($HOUR==02) { $SESS="c";}
if ($HOUR==03) { $SESS="d";}
if ($HOUR==04) { $SESS="e";}
if ($HOUR==05) { $SESS="f";}
if ($HOUR==06) { $SESS="g";}
if ($HOUR==07) { $SESS="h";}
if ($HOUR=="08") { $SESS="i";}
if ($HOUR=="09") { $SESS="j";}
if ($HOUR==10) { $SESS="k"; }
if ($HOUR==11) { $SESS="l"; }
if ($HOUR==12) { $SESS="m"; }
if ($HOUR==13) { $SESS="n"; }
if ($HOUR==14) { $SESS="o"; }
if ($HOUR==15) { $SESS="p"; }
if ($HOUR==16) { $SESS="q"; }
if ($HOUR==17) { $SESS="r"; }
if ($HOUR==18) { $SESS="s"; }
if ($HOUR==19) { $SESS="t"; }
if ($HOUR==20) { $SESS="u"; }
if ($HOUR==21) { $SESS="v"; }
if ($HOUR==22) { $SESS="w"; }
if ($HOUR==23) { $SESS="x"; }

# extract doy and 2 digit year 
my $gweek;
$yy = substr($YEAR,2,2);
$doy=daynr($YEAR,$MONTH,$DAY);
$gweek=int( (($YEAR-1980)*365+int(($YEAR-1980)/4)+$doy-5)/7 );
if ($YEAR==2008) {$gweek=int( (($YEAR-1980)*365+int(($YEAR-1980)/4)+$doy-6)/7 );}

# end of date calculations

$DOY = sprintf("%03d",$doy);
$YY = sprintf("%02d",$yy);
#print " ddd is $DOY\n month is $month\n year is $year\n day is $mday\n yy is $YY\n gpsweek is $gpsw\n and the gpsweeknumberday is $gpsd\n also dnum since 80 is $dnum\n";
print " $DOY $SESS $YY $gweek \n";
# ###############################################
