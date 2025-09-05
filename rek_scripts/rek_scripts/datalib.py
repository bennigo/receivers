'''
datalib.py -- GPS data manipulation library

Created by Fjalar Sigurdarson.
Copyright (c) 2015-2016 Icelandi Met Office - http://en.vedur.is. All rights reserved.
'''

# third party imports
import argparse, os, sys, signal, glob, shutil
import logging
import logging.handlers
from datetime import datetime
from datetime import date
import hashlib
import urllib
from dateutil.relativedelta import relativedelta
import subprocess, time


def months_backwards(months):

    # List of set of months backwards (in year-month format)
    months_backwards = []

    # Name of months
    name_of_months = {1:'jan',2:'feb',3:'mar',4:'apr',5:'may',6:'jun',7:'jul',
                    8:'aug',9:'sep',10:'oct',11:'nov',12:'dec',}

    for i in range(-months+1, 1):

        d = datetime.now() + relativedelta(months=i)

        # Dictionary for a singe year-month
        month_backward = {'year':'', 'month_number':'', 'month_name':''}

        month_number = d.month
        month_name = name_of_months[month_number]
        year = d.year

        if month_number < 10:
            month_backward['year']          = str(year)
            month_backward['month_number']  = str(0)+str(month_number)
            month_backward['month_name']    = month_name
        else:
            month_backward['year']          = str(year)
            month_backward['month_number']  = str(month_number)
            month_backward['month_name']    = month_name

        months_backwards.append(month_backward)

    return months_backwards

def day_of_year(yyyymmdd):
    #print yyyymmdd

    year = int(yyyymmdd[0:4])
    month = int(yyyymmdd[4:6])
    day = int(yyyymmdd[6:8])

    day_info = date(year,month,day)
    day_number = day_info.timetuple()[7]

    if  day_number < 10:
        day_number = str(0)+str(0)+str(day_number)
    elif day_number >= 10 and day_number < 100:
        day_number = str(0)+str(day_number)

    return day_number

def get_files(frequency_letter, destination, station_id, month_backward, data_type):

    file_list = []
    year = month_backward['year']
    month = month_backward['month_name']

    if frequency_letter == 'a':
        frequency = '15s_24hr'
    elif frequency_letter == 'b':
        frequency = '1Hz_1hr'
    elif frequency_letter == 'c':
        frequency = '5Hz_1hr'
    elif frequency_letter == 'd':
        frequency = '15s_8hr'
    elif frequency_letter == 'e':
        frequency = '30s_24hr'
    elif frequency_letter == 'f':
        frequency = '10Hz_1hr'
    elif frequency_letter == 'h':
        frequency = '20Hz_1hr'
    else:
        print "ERROR: Frequency type not defined: %s" % frequency_letter
        sys.exit()

    # This switching between the old path system and the new one gives
    # certain flexibity while data is being phased to the new structure
    path = destination + '/' + year + '/' + month + '/' + station_id + '/' +  frequency + '/' + data_type

    try:
        files = os.listdir(path)

        for one_file in files:
            file_dict = {'name':'','size':'','md5':''}
            file_dict['name'] = one_file
            file_dict['size'] = os.path.getsize(path+'/'+one_file)
            file_dict['md5'] = hashlib.md5(file_dict['name']+str(file_dict['size'])).hexdigest()

            file_list.append(file_dict)

    except Exception as error:

        print error
        # should here be a hard exit?

    print '%s\t list length for month %s %s: %s' % (data_type, month, year, len(file_list))
    print ''

    return file_list

def make_directory(destination, year, month, station_id, frequency, data_type):

    # Create the path in this order
    path = [year, month, station_id, frequency, data_type]

    for folder in path:
        destination = destination + '/' + folder
        if not os.path.isdir(destination):
            print 'Destination %s does not exist. Creating folder %s ' % (destination, folder)
            os.mkdir(destination)

