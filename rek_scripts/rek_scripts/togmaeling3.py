#!/usr/bin/python
from __future__ import print_function
"""
    general program to make an aqusisison on extension-meter
    can print the raw format directly or save to structured fila and database
    Author: Benni, bgo@vedur
    Date: 23.09.2018
"""


def extention_measurement(port=6785, server="localhost", baudrate=34800, channel=1, timeout=3, printraw=False):
    """
    """
    
    from datetime import datetime as dt
    import serial
    import re
    import ast

    serialconnection="socket://{}:{}".format(server, port)
    ser = serial.serial_for_url(serialconnection,baudrate,timeout=timeout)

    regex = re.compile('AVW2XX|1|2')
    callstring = b'\r\r\r\r{}\r'.format(channel)
    
    currtime = dt.now()
    ser.write(callstring)

    measurement = ser.readlines()
    if printraw:
        #print("--------------------")
        print("datetime =  {} ".format(currtime.strftime("%Y-%d-%m %H:%M:%S")))
        for x in measurement:
            x = x.rstrip()
            if x and not regex.match(x):
                x = x.rstrip()
                print(x)
        print("====================")

    measurement_dict = {}
    measurement_dict.update( {"datetime": (currtime,) })
    for x in measurement:
        x = x.rstrip()
        if x and not regex.match(x):
            for y in x.split(','):
                key = y.split("=")[0].strip() 
                value = y.split("=")[1].strip() 
                value = list( value.split() )
                if key != "ExciteVolts":
                    value[0] = ast.literal_eval(value[0])
                measurement_dict.update( { key: tuple(value) } )

    ser.close()

    return measurement_dict


def save_measurement(measurement_dict,file_name="togmaeling", path='.'):
    """re
    """
    
    from datetime import datetime as dt
    import csv
    import os
    
    
    # reorganizing the dictionary for printing only values to the file
    measurement_dict={ key: value[0] for key, value in measurement_dict.items() }

    # timestamp for file name
    timestamp = measurement_dict.pop('datetime')
    measurement_dict['date time'] = timestamp.strftime("%Y-%m-%d %H:%M:%S") 

    
    #file name
    suffix="dat"
    datafile="{}{}-{}".format(file_name, measurement_dict['Channel'], timestamp.strftime("%Y%m"))
    
    if not os.path.exists(path):
        os.makedirs(path)

    datafile=os.path.join(path, datafile + "." + suffix)

    with open(datafile, mode='a') as csv_file:
        fieldnames = ['date_time', 'Channel', 'MuxChan', 'BeginFreq', 'EndFreq', 'ExciteVolts',
                      'Freq_Value', 'Peak_Amplitude', 'Signal-Noise_Ratio', 'Noise_Freq', 'Decay_Ratio',
                      'Therm_Resistance' ]

        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, delimiter ='\t')
        if os.stat(datafile).st_size == 0:
            writer.writeheader()
        writer.writerow(measurement_dict)

def archive():
    """
    Not implemented
    """

    pass

def measure(args):
    """
    """

    measurement_dict = extention_measurement(server=args.server, port=args.port, 
                                   baudrate=args.baudrate, channel=args.channel, 
                                   timeout=args.timeout, printraw=args.raw )

    return measurement_dict

def download(args):
    """
    """


    print("ARGS: {}".format(args))
    import os
    import filecmp
    import pandas as pd

    from datetime import datetime as dt
    from shutil import copy
    
    import comfunc


    st_id=args.st_id
    file_list=["vw1.csv","vw2.csv","vw3.csv","messages"]
    remote_path_list=["/persistent/lib/logger-csv/", "/persistent/lib/logger-csv/", 
                      "/persistent/lib/logger-csv/", "/persistent/log/"]

    files_to_download=dict(zip(file_list,remote_path_list))

    ftp = comfunc.ftp_open_connection(args.server, args.port, user="root", passwd="root", pasv=False)

    #tmpdir="/home/bgo/extension_meter/tmp/"
    tmpdir="/mnt/datadiskur/extension_meter/tmp/"
    downloaded_files = comfunc.ftp_download(files_to_download, tmpdir, local_id=st_id, clean_tmp=args.clean, ftp=ftp, pasv=False, ftp_close=False, sbase=r'[56].csv')
    

    # backing up the original downloaded files and copying them to a folder
    currtime = dt.now()

    year = currtime.strftime("%Y/%b")

    backup_files= []
    #backupdir_pre="/home/bgo/extension_meter/cautus_backup/"
    backupdir_pre="/mnt/datadiskur/extension_meter/cautus_backup/"
    backupdir = os.path.join(backupdir_pre, year, st_id )

    if not os.path.exists(backupdir):
        os.makedirs(backupdir)

    #downloaded_files=file_list # temporary untill online
    for f in downloaded_files:
        
        fdir, fname = os.path.split( f ) 
        basen, ext = os.path.splitext(fname)
        bfile="{}_{}{}".format( basen, currtime.strftime("%Y%m%d%H"),ext)
        fpathname=os.path.join(backupdir, bfile)
        copy(f,fpathname)

        if filecmp.cmp(f, fpathname) == True:
            backup_files.append(fpathname)

    
    print(backup_files)
    #copy()
    if args.subcommand == "collecting":
        columns=['date', 'time','Freq_Value','T']
        for fil in backup_files[0:-1]:
            fil = os.path.join(".", fil)
            data = pd.read_csv(fil, sep=";", parse_dates=[['date', 'time']],  names=columns, index_col=[0], dayfirst=True)
            print(data.head())

        print("COLLECTING")
    

    measurement_dict = {}

    
    print(args)


    return measurement_dict

def main():
    """
    """

    import argparse
    import sys

    parser = argparse.ArgumentParser( description="Download or Make extension-meter measurements" )

    # server options
    server_options=parser.add_argument_group(title='Server options')
    server_options.add_argument('-s', '--server', type=str, default='localhost', help='Host: Defaults to localhost' )
    server_options.add_argument('-p', '--port', type=int, default=6785, help='Port: Defaults to 6785' )
    server_options.add_argument('-t', '--timeout', type=int, default=4, help='Connection timeout: Defaults to 3 (sec)' )
    
    # Saving options 
    parser.add_argument('--archive', action="store_true", help='Archive the data' )
    parser.add_argument('--path', type=str, default='.', help='File path to store file in: Defaults to cwd' )
    parser.add_argument('--save', type=str, 
                                  help='Store the data to a file: Default name togmaeling[channel]-YYYYmm.dat. replace with custom prefix' )
    parser.add_argument('--db', action="store_true", help='Store the data to a database' )

    # making subcommands
    subparsers=parser.add_subparsers(title='Subcommands', description='valid subcommands', dest='subcommand')

    # For taking a direct measurement
    measurement_options=subparsers.add_parser("measure", help="Take extentiometer measurement")

    measurement_options.add_argument('-b', '--baudrate', type=int, default=34800, help='Bautrate: Defaults to 38400' )
    measurement_options.add_argument('-ch', '--channel', type=int, default=1, help='Measurement Channel: Defaults to 1' )
    measurement_options.add_argument('--raw', action="store_true", help='Print raw format as it prints to the serial console' )
    measurement_options.set_defaults(func=measure)
   
    # for downloading
    download_options=subparsers.add_parser("download", help="Downloading acquasition files")

    
    #download_options.add_argument('-f', '--file', action="store_true")
    download_options.add_argument('-u', '--user', type=str, default='root', help='login for remote host')
    download_options.add_argument('-p', '--passwd', type=str, default='root', help='password for remote host')
    download_options.add_argument('-c', '--clean', action="store_true", help='Remove temporary fils alreadin in directory')
    download_options.add_argument('-s', '--st_id', type=str, default="", help='id for the station')

    args, sub_commands = parser.parse_known_args()
   
    if sub_commands:

        collecting_sub = download_options.add_subparsers(title='collecting', help='collect help', dest='subcommand')
        #collecting_sub.required = False
        #print(collecting_sub.required)
        collecting_options = collecting_sub.add_parser('collecting', help='collect help')
        collecting_options.add_argument('-ch', '--channel', type=int, default=1, help='Measurement Channel: Defaults to 1' )
    
    download_options.set_defaults(func=download)



    args = parser.parse_args()
    
    #if args.measure:
    #print("ARGS: {}".format(args))
    measurement_dict=args.func(args)


    if args.save:
        if measurement_dict:
            save_measurement(measurement_dict, file_name=args.save, path=args.path)
        else:
            print("Nothing saved to file {}: No new measurements found".format(args.save))

    if args.db:
        print("save to database, not implemented", file=sys.stderr, path=args.path)

    if args.archive:
        print("Archive the data, not implemented", file=sys.stderr, path=args.path)

if __name__ == '__main__':
    main()
