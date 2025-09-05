#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
   gps2influx.py -- GPS healt metrics export from Postgresql to Influx/Grafana

   Created by Fjalar Sigurdarson.
   Copyright (c) 2017 Icelandi Met Office - http://en.vedur.is. All rights reserved.
   """

# ###############################
#
# gps2influx.py
# Code made by fjalar@vedur.is
# Iceland Met Office
# 2017
#
# ###############################

# Import modules
import os,sys, argparse
from time import time, sleep
from datetime import timedelta, datetime
#DBs
from influxdb import InfluxDBClient
import psycopg2, signal

# Methods
def readFromPsql(unit, value):
    ''' Read GPS health metrics from the PSQL database '''

    ## PSQL DB info
    # FIX: move to config file
    dbname = 'gps'
    user = 'gps'
    host = 'rek2.vedur.is'
    password = 'GPS123ops'

    ## Dict for all results
    all_records ={}

    # Try connect to database
    try:
        print '>> Connecting to database "{0}" with user "{1}" on host "{2}"'.format(dbname, user, host)
        conn = psycopg2.connect("dbname={0} user={1} host={2} password={3}".format(dbname,user,host,password))
        cursor = conn.cursor()
        print '>> Conneciton established!\n'
    except:
        print "I am unable to connect to the database"
        sys.exit(1)

    # Fetch a station list for currently active stations
    query_station_list = '''select marker from stations where date_to is null;'''
    cursor.execute(query_station_list)
    stations = cursor.fetchall()

    # DEBUG SETTINGS
    #stations = (('AUST'),('GFUM'),('VMEY'))

    # Loop through the stations and fetch values
    for station in stations:
        
        # results from fetchall() are in tuples.. fixing that..
        # DEBUG: comment the line here below when debugging with the
        # stations = (('AUST'),('GFUM'),('VMEY')) list.
        station = station[0]

        if unit == 'hours':
            query = '''select timestamp, rout_stat, recv_stat, recv_temp,recv_volt from checkcomm where sid = '{0}' and timestamp > now() - interval '{1} hours';'''.format(station, value)        
            print '>> Querying for the last {0} hours of measurements for station {1}'.format(value, station)
        elif unit == 'days':
            query = '''select timestamp, rout_stat, recv_stat, recv_temp,recv_volt from checkcomm where sid = '{0}' and timestamp > now() - interval '{1} days';'''.format(station, value)        
            print '>> Querying for the last {0} days of measurements for station {1}'.format(value, station)
        else:
            print '>> Unit for query not known...'
            sys.exit(1)

        try:
            cursor.execute(query)
        except Exception as e:
            raise e
        
        # retrieving all records
        records = cursor.fetchall()
        #print records
        if len(records) is not 0:
            all_records[station] = records
        else:
            print '>> No records for station {0}. Moving along...'.format(station)
        
    return all_records
 
def prepForInflux(all_records):
    ''' Prepare the metrics by packing them into a JSON structure '''

    # instantiate a json variable
    json_body = []

    for key,values in all_records.iteritems():


        # convert tuple to list. This is needed for the next loop below where 0 is interchanged
        # with None
        values_new = []

        for value in values:
            value_list = []
            for v in value:
                value_list.append(v)
            values_new.append(value_list)

        for value in values_new:
                        
            # Creating each datapoint (of four) as JSON
            for index,val in enumerate(value):
                #print index,val
                if (index == 3 or index == 4) and val == 0:
                    #print 'Found value 0'
                    value[index] = None
                    #print 'New value: {0}'.format(value)

            #print '{0} {1}/{2}/{3}/{4}/{5}'.format(key,value[0],value[1],value[2],value[3],value[4])

            # Router status metrics
            json_body.append(
            {
                "measurement":'router_status',
                "tags": {
                    "Location": key,
                    "Unit": 'Boolean',
                },
                "time": value[0].isoformat(),
                "fields": {
                    "value": str(value[1])
                }
            })

            # Receiver status metrics
            json_body.append(
            {
                "measurement":'receiver_status',
                "tags": {
                    "Location": key,
                    "Unit": 'Boolean',
                },
                "time": value[0].isoformat(),
                "fields": {
                    "value": str(value[2])
                }
            })

            # Temperature metrics
            json_body.append(
            {
                "measurement":'temperature',
                "tags": {
                    "Location": key,
                    "Unit": 'Temp',
                },
                "time": value[0].isoformat(),
                "fields": {
                    "value": str(value[3])
                }
            })

            # Voltage metrics
            json_body.append(
            {
                "measurement":'voltage',
                "tags": {
                    "Location": key,
                    "Unit": 'Volt',
                },
                "time": value[0].isoformat(),
                "fields": {
                    "value": str(value[4])
                }
            })


            # Monitor json_body size and ship when certain size has been reached
            if (len(json_body) > 30000):
                #print len(json_body)
                print("Shipping to influx")
                #shipToInflux()
                print("Shipping complete")
                json_body = []

        # Ship the final (and possibly only) json to InFlux
        print("Shipping to influx")
        shipToInflux(json_body)
        print("Shipping complete")

def shipToInflux(json_body):
    ''' Ship the JSON packages to the InFlux DB.'''

    # InFlux DB info
    # FIX: move to config file
    host = 'influxdb.vedur.is'
    port = 8086
    dbname = 'gps_metrics'
    user = 'gpsops'
    password = 'pass4gps'

    try:
        # Ship to Influx
        print '>> Connecting to InFlux database "{0}" with user "{1}" on host "{2}"'.format(dbname, user, host)
        client = InfluxDBClient(host, port, user, password, dbname)
        print '>> Now shipping data...'
        client.write_points(json_body)
    except Exception as e:
        print '>> Shipping failed...'
        raise e

def progInfoScreen():
    ''' Program splash screen.'''

    today = datetime.now().strftime("%A %d. %B %Y - %H:%M:%S")

    print ""
    print "Copyright (c) 2017 Icelandic Met Office"
    print "gps2Influx 0.1 (August 2017)"
    print "# {} #".format(today) 
    print ""

def exit_gracefully(signum, frame):
    # restore the original signal handler as otherwise evil things will happen
    # in raw_input when CTRL+C is pressed, and our signal handler is not re-entrant
    signal.signal(signal.SIGINT, original_sigint)

    try:
       if raw_input("\nReally quit? (y/n)> ").lower().startswith('y'):
           sys.exit(1)

    except KeyboardInterrupt:
       print("Ok ok, quitting")
       sys.exit(1)

    # restore the exit gracefully handler here
    signal.signal(signal.SIGINT, exit_gracefully)

    # Method borrowed from:
    # http://stackoverflow.com/questions/18114560/python-catch-ctrl-c-command-prompt-really-want-to-quit-y-n-resume-executi

def main():
    ''' Main '''

    # This is used to catch Ctrl-C exits
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, exit_gracefully)

    # Display some nice program info
    progInfoScreen()

    # Instantiate argparser
    parser = argparse.ArgumentParser()

    # Setup the argument parser
    parser.add_argument('-o','--hours', type=int,
                        help='Number of hours to be fetched and sent to Influx')
    parser.add_argument('-d','--days', type=int,
                        help='Number of days to be fetched and sent to Influx')

    # Fetch the arguments
    args = parser.parse_args()

    # Set timer for the operation
    timer_start = time()

    if args.hours:
        records = readFromPsql('hours',args.hours)
    elif args.days:
        records = readFromPsql('days',args.days)    
    else:
        print 'Arguments missing. Please use -h / --help for help.'
        sys.exit(1)
    
    prepForInflux(records)

    timer_stop = time()
    time_ended = datetime.now().strftime("%H:%M:%S")

    print "-----------------------------------------------------------------------"
    print "Import to InFlux finished {0}. Time elapsed: {1:.1f} seconds".format(time_ended, timer_stop-timer_start)
    print "-----------------------------------------------------------------------"

if __name__ == '__main__':
    main()