#!/bin/sh
# Hourly and daily download of Trimble NETRS receiver HEKR
# HG sep 2006; modified for 24hr15sec+1hr1sec data
# HG dec 2006: læt setja skrár í tmp og færa svo yfir
# HG sep 2007: Modified from GOLA for v-lan
# HG sep 2007: Mirror receiver directory using wget -Nr
# HG dec 2009: Use dump instead of brot
# HG mar 2010: put 1hr 1 sec data on dump
# HG jun 2011: replaced by get_ip_ab.sh
#################

/home/gpsops/bin/get_ip_ab.sh HESA
