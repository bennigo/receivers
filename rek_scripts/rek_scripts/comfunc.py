#!/usr/bin/python

# ###############################
#
# comfunc.py 0.1
# Code made by bgo@vedur.is
# Iceland Met Office
# 2019
#
# ###############################


"""

In this program are the following functions in order:

_download_with_progressbar(ftp, remote_file,local_file, remote_file_size, offset=0)
"""


def make_file_name(station_id, day, session="15s_24hr", receiver_type="POLARX5", ftype="IMOstd", compression=".gz"):
    """
    """

    import gtimes.timefunc as gt
    import re

    file_name = ""
    suff_dict={
            "POLARX5": "sbf",
            }

    
    daysession = re.compile("24hr",re.IGNORECASE)
    hoursession = re.compile("1hr",re.IGNORECASE)
    
    if ftype == "IMOstd":

        print("Session name: {}".format(session))
        if daysession.search(session): filedate = day.strftime("%Y%m%d0000a")

        if hoursession.search(session): filedate = day.strftime("%Y%m%d%H00b")

        file_name = "{0}{1}.{2}{3}".format(station_id, filedate, suff_dict[receiver_type], compresssion)

    if not file_name:
        print("The fromat {0} is unknown".format(ftype))
        
    return file_name


def ftp_open_connection(ip_number,ip_port, user="anonymous", passwd="", pasv=True, timeout=10):

    """
        open ftp connection
    """

    from ftplib import FTP
    
    ## Try to connect to the server

   # TEMP stuff sometimes we need passive ftp will go to config

    try:
        print( "Connecting to station...")
        ftp = FTP()
        connect_res = ftp.connect(ip_number, ip_port, timeout=timeout)
        login_res = ftp.login(user=user,passwd=passwd)

        ftp.set_pasv(pasv)
        print( "Connection successful!")
    except: 
        print( "Connection failed")

        #ftp = None 

    return ftp


def ftp_download(files_dir_to_download_dict, local_dir, local_id="",clean_tmp=True,
        ftp=None, ip_number=None, ip_port=None, user="anonymous", passwd="", pasv=True, ftp_close=True, sbase=[0,11]):
    """
    download a list of files from an ftp server
    """

    import os
    import re
    from ftplib import FTP

    if not ftp:
        ftp = ftp_open_connection(ip_number ,ip_port,user="anonymous", passwd="", pasv=pasv)

    if not ftp:
        print( "Can't connect to {}:{}, nothing downloaded".format(ip_number ,ip_port) )
        return []
  
    downloaded_files = []
    remote_file_size = {}

    # Execute file download if connection was succsesfull 
    for file_name, remote_dir in sorted(files_dir_to_download_dict.items(),reverse=True):
 
        print("=====================================")
        print("File name: {}".format(file_name))
        print("Remote directory: {}".format(remote_dir))
        print("Download directory: {}".format(local_dir))
        print("-------------------------------------")
        print(">")

        file_base,file_ext = os.path.splitext(file_name)

        if local_id:
            local_file = "{0}{1}_{2}{3}".format(local_dir,file_base,local_id,file_ext)
        else:
            local_file = "{0}{1}{2}".format(local_dir,file_base,file_ext)

        if clean_tmp is True and os.path.isfile(local_file):
            os.remove(local_file)

        remote_file= "{0}{1}".format(remote_dir,file_name)
    
        offset = 0 
        if os.path.isfile(local_file):
            # check how much has alread been downloaded
                offset = os.path.getsize(local_file)

        
        # Download the file
        print( 'Downloading ' + file_name )

        # check if remote_file exists and return the size.
        try:
            remote_file_size = ftp.size(remote_file)
        except:

                remote_file_dict = ftp_list_dir([remote_dir], ftp=ftp, ftp_close=False)
                print(remote_file_dict)
                if type(sbase) is list: 
                    basename = file_name[sbase[0]:sbase[1]]
                else:
                    basename = sbase

                base_regexp = re.compile(basename)

                
                print("File {} not on recever listing remote files".format(remote_file))
                print(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
                for rdir, rfile_list in remote_file_dict.items():
                    for rfile in rfile_list:
                        print(rfile) 
                print(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
                print(">")

                for rdir, rfile_list in remote_file_dict.items():
                    for rfile in rfile_list:
                        file_name=rfile.split()[-1]
                        
                        local_file = "{0}{1}".format(local_dir,file_name)
                        if os.path.isfile(local_file):
                            # check how much has alread been downloaded
                            offset = os.path.getsize(local_file)
    
                        if base_regexp.search(file_name):
                            remote_file="{0}{1}".format(rdir,file_name)
                            print("Found file {0} on receiver will download to \n {1}".format(remote_file, local_file) +
                                  " File will not be archived automatically." ) 
                            remote_file_size = ftp.size(remote_file)
                            diff = _download_with_progressbar(ftp, remote_file,local_file, remote_file_size, offset=offset)
                        else:
                            print("Did not find any file matching {0} on the receiver, \n".format(basename) + 
                                  "check if the receiver or station info is configured correctly")
                    
                
                continue

        print( "FTP: {}".format(ftp) )
        diff = _download_with_progressbar(ftp, remote_file,local_file, remote_file_size, offset=offset)
        print("Difference between remote and downloaded file: {0:d}".format(diff))
        if diff == 0:
            downloaded_files.append(local_file)

    if ftp_close:
        ftp.close()

    return downloaded_files

def ftp_list_dir(dir_list, ftp=None, ip_number=None, ip_port=None, pasv=True, ftp_close=True):
    """
    """

    import os
    import re
    from ftplib import FTP

    if not ftp:
        ftp = ftp_open_connection(ip_number ,ip_port, pasv=pasv)

    if not ftp:
        print( "Can't connect to {}:{}, nothing downloaded".format(ip_number ,ip_port) )
        return []
    
    remote_file_dict = {}
    for remote_dir in dir_list:
        remote_dir_list=[]
        ftp.dir(remote_dir, remote_dir_list.append)
        remote_file_dict[remote_dir] = remote_dir_list
    

    if ftp_close:
        ftp.close()

    return remote_file_dict


def is_gz_file(filepath):
    """
    Check if a file is a gzip file
    """

    import binascii
    
    with open(filepath, 'rb') as test_f:
        return binascii.hexlify(test_f.read(2)) == b'1f8b'



def _download_with_progressbar(ftp, remote_file,local_file, remote_file_size, offset=0):
    """
    Download a file using a process bar. 
    Returns a the difference in bytes between the remote file and the downloaded file
    """

    import progressbar
    import os
    
    progress = progressbar.AnimatedProgressBar(start=offset,end=remote_file_size, width=50)

    with open(local_file, 'ab') as f:
        def callback(chunk):
            f.write(chunk)
            progress + len(chunk)

            # Visual feedback of the progress!
            progress.show_progress()

        ftp.retrbinary('RETR {0}'.format(remote_file), callback,rest=offset)
        print("")
    # print( remote_file_size )
    local_file_size = os.path.getsize(local_file)    

    return local_file_size-remote_file_size
