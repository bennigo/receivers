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
    #callstring = b'\r\r\r\r'
    #   print(callstring)
    
    currtime = dt.now()
    ser.write(callstring)

    measurement = ser.readlines()
    # print(measurement)
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
        fieldnames = ['date time', 'Channel', 'MuxChan', 'BeginFreq', 'EndFreq', 'ExciteVolts',
                      'Freq_Value', 'Peak Amplitude', 'Signal-Noise Ratio', 'Noise Freq', 'Decay Ratio',
                      'Therm Resistance' ]

        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, delimiter ='\t')
        if os.stat(datafile).st_size is 0:
            writer.writeheader()
        writer.writerow(measurement_dict)

def archive():
    """
    Not implemented
    """

    pass

def main():
    """
    """

    import argparse
    import sys

    parser = argparse.ArgumentParser( description="Make extension-meter measurement" )

    parser.add_argument('-s', '--server', type=str, default='localhost', help='Host: Defaults to localhost' )
    parser.add_argument('-p', '--port', type=int, default=6785, help='Port: Defaults to 6785' )
    parser.add_argument('-b', '--baudrate', type=int, default=34800, help='Bautrate: Defaults to 38400' )
    parser.add_argument('-ch', '--channel', type=int, default=1, help='Measurement Channel: Defaults to 1' )
    parser.add_argument('-t', '--timeout', type=int, default=3, help='Connection timeout: Defaults to 3 (sec)' )
    parser.add_argument('--raw', action="store_true", help='Print raw format as it prints to the serial console' )
    parser.add_argument('--db', action="store_true", help='Store the data to a database' )
    parser.add_argument('--archive', action="store_true", help='Store the data' )
    parser.add_argument('--save', type=str, help='Store the data to a file: Default name togmaeling[channel]-YYYYmm.dat. replace with custom prefix' )
    parser.add_argument('--path', type=str, default='.', help='File path to store file in: Defaults to cwd' )
    
    args = parser.parse_args()
    #print(args)

    measurement_dict = extention_measurement(server=args.server, port=args.port, 
                                   baudrate=args.baudrate, channel=args.channel, 
                                   timeout=args.timeout, printraw=args.raw )

    if args.save:
        save_measurement(measurement_dict, file_name=args.save, path=args.path)

    if args.db:
        print("save to database, not implemented", file=sys.stderr, path=args.path)

    if args.archive:
        print("Archive the data, not implemented", file=sys.stderr, path=args.path)

if __name__ == '__main__':
    main()
