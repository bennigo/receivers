# -*- coding: iso-8859-15 -*-
"""
"""

import logging
from os import listdir


def tqdmWrapViewBar(*args, **kwargs):
    from tqdm import tqdm

    pbar = tqdm(*args, **kwargs)  # make a progressbar
    last = [0]  # last known iteration, start at 0

    def viewBar2(a, b):
        pbar.total = int(b)
        pbar.update(int(a - last[0]))  # update pbar with increment
        last[0] = a  # update last known iteration

    return viewBar2, pbar  # return callback, tqdmInstance


def createSSHClient(server, port, user, password, ssh_keys=True, logging_level=logging.WARN):
    """"""
    import paramiko

    # handling logging
    logging.basicConfig(
        format="%(asctime)-15s [%(levelname)s] %(funcName)s: %(message)s",
        level=logging_level,
    )
    logging.getLogger().setLevel(logging_level)
    module_logger = logging.getLogger()
    module_logger.info(
        "server: {}, port: {}, user: {}, passw: {}".format(server, port, user, password)
    )

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(server, port, user, password, look_for_keys=ssh_keys)

    return client


def md5sum(file_name):
    """"""
    import hashlib

    with open(file_name, "rb") as f:
        file_hash = hashlib.md5()
        while chunk := f.read(8192):
            file_hash.update(chunk)

    return file_hash


def makeBackup(localPath):
    """"""
    import configparser
    from pathlib import Path
    from scp import SCPClient

    config = configparser.ConfigParser()
    config.read("config.cfg")

    user = config["BACKUP"]["user"]
    password = config["BACKUP"]["password"]
    server = config["BACKUP"]["server"]
    port = config["BACKUP"]["port"]
    remotebackupPath = config["BACKUP"]["remotepath"]
    remoteDir = Path(localPath).parts[-5:]
    remoteFile = str(Path(remotebackupPath).joinpath(*remoteDir))

    ssh = createSSHClient(server, port, user, password, logging_level=logging.WARNING)
    sftp = ssh.open_sftp()
    sftp.chdir(remotebackupPath)
    for dir in list(remoteDir[:-1]):
        try:
            sftp.chdir(dir)
        except:
            sftp.mkdir(dir)
            sftp.chdir(dir)

    print("Archiving to: {}:{}".format(server, localPath))
    viewBar, pbar = tqdmWrapViewBar(ascii=True, unit="b", unit_scale=True)
    sftp.put(localPath, remoteFile, callback=viewBar)
    pbar.close()
    sftp.close()

    # get the remote md5sum
    _, stdout, _ = ssh.exec_command("md5sum " + remoteFile)
    remote_hash = stdout.read().decode("ascii").split()
    ssh.close()

    return remote_hash


def sftpDownload(
    conn, remotePath, localPath, rmRemote=False, backup=False, logging_level=logging.WARNING
):
    """ """
    from pathlib import Path

    # handling logging
    logging.basicConfig(
        format="%(asctime)-15s [%(levelname)s] %(funcName)s: %(message)s",
        level=logging_level,
    )
    logging.getLogger().setLevel(logging_level)
    module_logger = logging.getLogger()

    Path(localPath).parent.mkdir(parents=True, exist_ok=True)
    # get the remote md5sum
    _, stdout, _ = conn.exec_command("md5sum " + remotePath)
    remote_hash = stdout.read().decode("ASCII").split()

    sftp = conn.open_sftp()

    downl = True
    if Path(localPath).is_file():
        local_hash = md5sum(localPath).hexdigest()
        if local_hash == remote_hash[0]:
            module_logger.info("Remote file: {0}".format(remote_hash[1]))
            module_logger.info("Local file {0}".format(localPath))
            module_logger.warning("Nothing to download: Remote and local files are identical")
            downl = False
        else:
            module_logger.warning("Files are not identical will download again")

    if downl:
        print("="*50)
        print("Downloading: {}...".format(remotePath))
        viewBar, pbar = tqdmWrapViewBar(ascii=True, unit="b", unit_scale=True)
        sftp.get(remotePath, localPath, callback=viewBar)
        pbar.close()
        local_hash = md5sum(localPath).hexdigest()
        if local_hash == remote_hash[0]:
            module_logger.info("Remote file: {0}".format(remote_hash[1]))
            module_logger.info("Local file {0}".format(localPath))
            module_logger.warning("Remote and local files are identical")

            if backup:
                backup_hash = makeBackup(localPath)
                if local_hash == backup_hash[0]:
                    module_logger.info("Remote file: {0}".format(backup_hash[1]))
                    module_logger.info("Local file {0}".format(localPath))
                    module_logger.warning("Archive and local files are identical")

                if rmRemote:
                    module_logger.warning("Removing: {}".format(remotePath))
                    # sftp.remove(remotePath)
        sftp.close()

    return localPath


def gpsRollover(t=None, forward=False):
    """
    Rollover start date:  23:59:42 UTC on April 6, 2019
    """
    from datetime import datetime as dt
    from datetime import timezone as tz

    rollStart = dt(
        year=2019, month=4, day=6, hour=23, minute=59, second=42, tzinfo=tz.utc
    )
    if t is None:
        t = rollStart
        # It does not make sence to go forward from this point in time
        forward = False

    rolloverTime = 7 * 1024 * 86400 + 5  # fixing rollover problem from 2019
    if forward:
        rolloverTime *= -1

    if isinstance(t, dt):
        t = dt.fromtimestamp(t.timestamp() - rolloverTime)
    if isinstance(t, float):
        t = dt.fromtimestamp(t - rolloverTime)

    return t


def listShuebox(conn, remotePath, logging_level=logging.WARNING):
    """"""

    import logging
    from datetime import datetime as dt
    from pathlib import Path
    from gtimes.timefunc import toDatetime

    # handling logging
    logging.basicConfig(
        format="%(asctime)-15s [%(levelname)s] %(funcName)s: %(message)s",
        level=logging_level,
    )
    logging.getLogger().setLevel(logging_level)
    module_logger = logging.getLogger()

    remoteFileList = []
    remoteDirList = []
    remote50HzList = []
    sftp = conn.open_sftp()
    dlist = sftp.listdir(remotePath)
    for ydir in dlist:
        if int(ydir) >= 1999 and int(ydir) <= gpsRollover(dt.now()).year:
            remoteyPath = str(Path(remotePath).joinpath(ydir))
            for mdir in sftp.listdir(remoteyPath)[:1]:
                remotemPath = str(Path(remoteyPath).joinpath(mdir))
                for mcont in sftp.listdir(remotemPath):
                    if "sac" in mcont:
                        remoteFileList.append(str(Path(remotemPath).joinpath(mcont)))
                    elif "log" in mcont:
                        pass
                    else:
                        remoteDirList.append(Path(remotemPath).joinpath(mcont))

    for dir in remoteDirList[:1]:
        module_logger.warning("Listing: {}".format(dir))
        for f in sftp.listdir(str(dir)):
            remote50HzList.append(str(dir.joinpath(f)))

    dformat = "%Y.%m.%d.%H.%M.%S"
    remoteFileList.sort(key=lambda x: toDatetime(x[-26:-7], dformat))
    remote50HzList.sort(key=lambda x: toDatetime(x[-30:-11], dformat))

    return remoteFileList, remote50HzList


def downloadFilesDict(stat, fileList, localPathPre, session, logging_level=logging.WARNING):
    """"""

    from pathlib import Path
    from collections import OrderedDict
    from gtimes.timefunc import datepathlist
    from gtimes.timefunc import toDatetime

    # handling logging
    logging.basicConfig(
        format="%(asctime)-15s [%(levelname)s] %(funcName)s: %(message)s",
        level=logging_level,
    )
    logging.getLogger().setLevel(logging_level)
    module_logger = logging.getLogger()

    remoteFileDict = OrderedDict()
    dformat = "%Y.%m.%d.%H.%M.%S"
    dtime = None
    lastdtime = None
    tmpFileList = []

    for f in fileList:
        module_logger.info("Original File: {}".format(f))
        fpath = Path(f).name.split(".")
        dtime = gpsRollover(toDatetime(".".join(fpath[2:8]), dformat), forward=True)
        # dtime = toDatetime(".".join(fpath[2:8]), dformat)
        sdtime = dtime.strftime(dformat)

        module_logger.info("lastdtime: {}, dtime {}".format(lastdtime, dtime))
        if lastdtime == dtime or lastdtime is None:
            module_logger.debug("lastdtime: {}, dtime {}".format(lastdtime, dtime))
            pathString = "{}/%Y/#b/{}/{}/{}.{}.{}".format(
                localPathPre,
                stat,
                session,
                ".".join(fpath[0:2]),
                sdtime,
                ".".join(fpath[8:]),
            )
            localPath = datepathlist(pathString, "1D", datelist=[dtime], closed="both")[0]
            tmpFileList.append((str(f), localPath))
            module_logger.debug("fileList: {}".format(tmpFileList))
            if lastdtime is None:
                remoteFileDict[dtime] = tmpFileList

        else:
            pathString = "{}/%Y/#b/{}/{}/{}.{}.{}".format(
                localPathPre,
                stat,
                session,
                ".".join(fpath[0:2]),
                sdtime,
                ".".join(fpath[8:]),
            )
            localPath = datepathlist(pathString, "1D", datelist=[dtime], closed="both")[0]
            tmpFileList.append((str(f), localPath))
            remoteFileDict[lastdtime] = tmpFileList
            tmpFileList = []

        lastdtime = dtime
    else:
        module_logger.debug("fileList: {}".format(tmpFileList))
        remoteFileDict[lastdtime] = tmpFileList


    return remoteFileDict


def main(logging_level=logging.WARNING):
    """"""

    import configparser
    from datetime import datetime as dt
    from datetime import timedelta as td

    # handling logging
    logging.basicConfig(
        format="%(asctime)-15s [%(levelname)s] %(funcName)s: %(message)s",
        level=logging_level,
    )
    logging.getLogger().setLevel(logging_level)
    module_logger = logging.getLogger()

    config = configparser.ConfigParser()
    config.read("config.cfg")
    sections = config.sections()
    sections.remove('BACKUP')
    stat = sections[0]

    user = config["DEFAULT"]["user"]
    password = config["DEFAULT"]["password"]
    port = int(config["DEFAULT"]["port"])
    localPathPre = config["DEFAULT"]["localpath"]

    server = config[stat]["server"]
    remotePath = config[stat]["remotepath"]

    ssh = createSSHClient(server, port, user, password, ssh_keys=False, logging_level=logging.WARNING)
    onesecFlist, remote50HzList = listShuebox(ssh, remotePath)
    module_logger.debug(onesecFlist)

    session = "default_rollover"
    remoteFileDict = downloadFilesDict(stat, onesecFlist, localPathPre, session, logging_level=logging.WARNING)
    datelist = sorted(remoteFileDict.keys())
    for date in datelist[0:1]:
        for f in remoteFileDict[date]:
            module_logger.info("{:>59}".format(f[0]))
            # module_logger.info("{:>59}, {}".format(f[0], f[1]))
            sftpDownload(ssh, f[0], f[1], backup=True, rmRemote=False, logging_level=logging.WARNING)

    # print(remote50HzList)
    session = "50Hz_rollover"
    remoteFileDict = downloadFilesDict(stat, remote50HzList, localPathPre, session, logging_level=logging.WARNING)
    datelist = sorted(remoteFileDict.keys())
    for date in datelist:
        for f in remoteFileDict[date]:
            module_logger.warning("{:>59}".format(f[0]))
            # module_logger.warning("{:>59}, {}".format(f[0], f[1]))
            sftpDownload(ssh, f[0], f[1], backup=True, rmRemote=False, logging_level=logging.WARNING)
    ssh.close()


if __name__ == "__main__":
    main()
