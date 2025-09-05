#!/usr/bin/python
# -*- coding: utf-8 -*-

def main():
    import cparser

    config_parser = cparser.Parser()
    
    print(config_parser.getStatus())
    station_info = config_parser.getStationInfo()
    print(station_info)

if __name__ == '__main__':
    main()

