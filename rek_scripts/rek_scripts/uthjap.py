#!/usr/bin/python

#import sys, os, datetime, gzip, zlib, argparse
#from cStringIO import StringIO
#from timefunc import shlyear
#import io

import os, argparse

import subprocess

def run_syscmd(check_cmd):
    ## Run command

    process = subprocess.Popen(check_cmd,shell=True,stdout=subprocess.PIPE)
    process.wait()

    proc_check_returncode = process.returncode
    proc_check_comm = process.communicate()[0].strip('\n')
    
    
    return proc_check_returncode,proc_check_comm


parser = argparse.ArgumentParser(description="program to convert between rinex formats")
parser.add_argument('files',nargs='+', help='List of files')

args = parser.parse_args()
print args

for file in args.files:
    dfile, Zext = os.path.splitext(file)
    fbase, dext = os.path.splitext(dfile)

    rfile = "%s%sO" % (fbase, dext[0:3])

    uncompr="gunzip -f %s" % file
    run_syscmd(uncompr)

    crx2rnx="crx2rnx - %s > %s" % (dfile, rfile) 
    run_syscmd(crx2rnx)
    os.remove(dfile)


#-#sta = sys.argv.pop(1)
#-#STA = sta.upper()
#-#YYYY = sys.argv.pop(1)
#-#doy = sys.argv.pop(1)
#-#scrpath = sys.argv.pop(1)
#-#
#-#YY = shlyear(int(YYYY))
#-#
#-#RfileDZ=STA+doy+"0."+YY+"D.Z"
#-#localD = os.path.join(scrpath,RfileDZ)
#-#remoteD = os.path.join("/home/sil/gps/rinex", YYYY, STA, RfileDZ)
#-#
#-#
#-#paramiko.util.log_to_file('/tmp/paramiko.log')
#-#transport = paramiko.Transport((hostname, ports))
#-#transport.connect(username = username, password = password)
#-#sftp = paramiko.SFTPClient.from_transport(transport)
#-#
#-#try:
#-#    print "Trying to download %s:%s" % (hostname,remoteD)
#-#    sftp.get(remotepath=remoteD, localpath=localD)
#-#except IOError as e:
#-#    print str(e)+ ": "+remoteD+" on "+hostname
#-#    sys.exit(0)
#-#sftp.close()
#-#transport.close()
#-#
#-#RfileD, fileExt = os.path.splitext(localD)
#-#rfile = os.path.join(os.path.dirname(RfileD), str(os.path.basename(RfileD)[0:-1]+"O").lower())
#-#
#-#uncompr="gunzip -f %s" % localD
#-#plib.run_syscmd(uncompr)
#-#
#-#crx2rnx="crx2rnx - %s > %s" % (RfileD, rfile) 
#-#plib.run_syscmd(crx2rnx)
#-#os.remove(RfileD)
#-#
#-#if os.path.isfile(rfile):
#-#    print "File: %s exists" % rfile
#-#
