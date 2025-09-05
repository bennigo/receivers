#!/usr/bin/python
# -*- coding: utf-8 -*-

# ------------------------------- #
#
# getSeptentrio.py 0.5
# Code made by bgo@vedur.is modified from fjalar@vedur.is
# Iceland Met Office
# 2018
#
# ------------------------------- #


def exit_gracefully(signum, frame):
    """ 
    Exit gracefully on Ctrl-C
    """
    
    current_func = sys._getframe().f_code.co_name + '() >> '

    # restore the original signal handler as otherwise evil things will happen
    # in raw_input when CTRL+C is pressed, and our signal handler is not re-entrant
    signal.signal(signal.SIGINT, original_sigint)

    try:
        if raw_input("\nReally quit? (y/n)> ").lower().startswith('y'):
            sys.exit(1)

    except KeyboardInterrupt:
        print 'Ok ok, quitting'
        sys.exit(1)

    # restore the exit gracefully handler here
    signal.signal(signal.SIGINT, exit_gracefully)

    # Method borrowed from:
    # http://stackoverflow.com/questions/18114560/python-catch-ctrl-c-command-prompt-really-want-to-quit-y-n-resume-executi


def main():
    
    import cparser
    
    parser = cparser.Parser()

    stalist = parser.getStationList()

    polarX5list = []
    for sta in stalist:
        if parser.getStationInfo(sta)['receiver']['type'] == "PolaRx5":
            polarX5list.append(sta)

    print(' '.join(str(x) for x in polarX5list))
    #print(polarX5list)
if __name__ == '__main__':

    import signal
    # This is used to catch Ctrl-C exits
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, exit_gracefully)

    main()
