#!/home/bgo/anaconda3/bin/python
# -*- coding: utf-8 -*-
"""
#/usr/bin/python
"""
from __future__ import print_function

import logging

def mask_first(x):

    """
    """
    import numpy as np

    result = np.ones_like(x)
    result[0] = 0 
    
    return result


def getmultigasdata(station, start=None, end=None, frfile=False, wrfile=True, fname=None):
    """
    """


    import requests
    import pandas as pd
    import datetime as dt
    strf="%Y-%m-%d %H:%M:%S"

    #pd.options.mode.chained_assignment = None

    if fname == None:
        fname = "{0:s}-multigas.dat".format(station)
    
    # ----------------- To get all the data needs to be fixed in the service -------------
    if start == None:
        start = dt.datetime.strptime("2012-12-01 00:00:00", "%Y-%m-%d %H:%M:%S") 
    if end == None:
         end = dt.datetime.now()
    
    dspan='?date_from={0:s}&date_to={1:s}'.format( start.strftime(strf), end.strftime(strf) )
    #print dspan
    # ------------------


    if frfile == True:
        data = pd.read_pickle(fname)

    else:
        url_rest = 'http://gas-zato.dev.vedur.is:11223/aot/gas/v2/stations/hekla/devices/multigas/observations'
        #url_rest = 'http://gas-zato.dev.vedur.is:11223/aot/gas/v2/stations/lagu/devices/crowcon/observations'
        url_rest = 'http://gas-zato.dev.vedur.is:11223/aot/gas/v2/stations/lagu/devices/crowcon/observations'
        url_rest = 'http://gas-zato.dev.vedur.is:11223/aot/gas/v2/stations/sojo/devices/crowcon/observations'
        #url_rest = 'http://dev-api01.vedur.is:11223/aot/gas/'
        station_marker = station
    
        #request = requests.get(url_rest+station_marker+'?date_from=2012-12-01 00:00:00&date_to=2017-12-31 00:00:00')
        request = requests.get(url_rest+station_marker+dspan)
        data = pd.DataFrame.from_records(request.json(),index='observation_time')
        data.index = pd.to_datetime(data.index)
        
        if wrfile == True:
            data.to_pickle(fname)
    
    #aq_grouped = data.groupby(['aquisition'])
    #mask = aq_grouped['aquisition'].transform(mask_first).astype(bool)
    mask = data.groupby(['acquisition'])['acquisition'].transform(mask_first).astype(bool)
    data = data.loc[mask]
    data.loc[:,'co2'] *= 0.0001 # data['co2'].multiply(0.0001)

    return data[start:end]


def open_datafile(filelist, file_type="rtk_coordinate", filt=[5]):
    """
    """
    df_list = []
    if file_type == "rtk_coordinate":
        col_names = ["date", "time",  "latitude",  "longitude",  "height",  "Q", "ns",  
                      "sdn",  "sde",  "sdu",  "sdne", "sdeu",  "sdun",  "age",  "ratio"]
    filelist.sort()

    df_list = [ pd.read_table(filename,  header=None, delimiter=r"\s+", names=col_names, 
                                    parse_dates=[["date", "time"]], index_col="date_time") 
                                    for filename in filelist if os.stat(filename).st_size != 0 ]
    df = pd.concat(df_list)
    df = df.convert_objects(convert_numeric=True)
    df = df.dropna()
    df.index = df.index.to_datetime()
    df = df.dropna()
    

    if filt and file_type == "rtk_coordinate":
        for i in filt:
            df = df[df.Q !=i]
    df = df.resample("S")

    return df

## extention meter stuff
## extention meter stuff

def calibration_param(sensor=None):
    """
    kvörðunarstuðlar og upphagsskilyrði RST gliðnunarmæla á Svínafellsheiði

    WM1255 / rás/channel shg1
    WM1252 / rás/channel shg2
    WM1254 / rás/channel shg3

    WM1253 / rás/channel sha1

    # inital measurement for WM1254 ------------------
    Nov  4 09:20:04 vwmeasure: VW3: Playing sweep with VAUX=5V, volume=41, then capturing waveform
    Nov  4 09:20:04 vwmeasure: VW3 temperature: Uref = 1.9645V, NTC = 12635.1 ohm, temp = -5.0 degC
    Nov  4 09:20:05 vwmeasure: VW3 freq: f = 1687.816 Hz, y = 2346617.573 (corr=0.107996)
    ------------------------


    """

    ch_parameters={
            "WM1255":
                { "channel": 1,
                  "name": "shg1",
                  "location": (64.01040,-16.80028,840),
                  "Cf": 0.045980,
	          "Fi": 1772.03660,
	          "Ri": 6608.09150,
    	          "A": 1.5278e-7,
	          "B": 0.044436,
	          "C": -129.26
                },
            
            "WM1252":
                { "channel": 2,
                  "name": "shg2",
                  "location": (64.01033,-16.80052,840),
                  "Cf": 0.045936,
	          "Fi": 1733.89680,
	          "Ri": 6880.29560,
    	          "A": 1.2156e-7,
	          "B": 0.044713,
	          "C": -128.61
                },
            "WM1254":
                { "channel": 3,
                  "name": "shg3",
                  "location": (64.01031,-16.80067,840),
                  "Cf": 0.045941,
	          "Fi": 1687.816,
	          "Ri": 12635.1,
    	          "A": 3.0300e-8,
	          "B": 0.045635,
	          "C": -130.86
                },
            "WM1253":
                { "channel": 4,
                  "name": "sha1",
                  "location": (64.00922,-16.82268,540),
                  "Cf": 0.045676,
	          "Fi": 2012.7395,
	          "Ri": 11025.728,
    	          "A": 5.0895e-8,
	          "B": 0.045162,
	          "C": -129.33
                }


           }

    for i in ch_parameters.keys():
        ch_parameters[i]["Li" ] = ch_parameters[i]["Fi"]**2/1000

    if sensor:
        return ch_parameters[sensor]
    else:
        return ch_parameters


def rstt(r):
    """
    """
    
    from math import log

    return 1./(1.4051e-3+2.369e-4*log(r)+1.019e-7*log(r)**3) - 273.2


def rstdl(sensor, FcRc):
    """
        estimate extention and temperature
        Using [["Freq_Value","Therm Resistance"]]
                         Fc                 Rc
    """
    
    param=calibration_param(sensor)
    
    Li = param["Li"]
    Rc = FcRc["Therm Resistance"]
    Ri = param["Ri"]

    Lc = FcRc["Freq_Value"]**2 / 1000

    dll = param["Cf"] * (Lc - Li) # breyting í mm, línuleg kvörðun
    dlp = (param["A"] * Lc**2 + param["B"] * Lc + param["C"]) \
                - (param["A"] * Li**2 + param["B"] * Li + param["C"])

    #K = (Lc * 0.000306 + -0.3186) * param["Cf"] # stroke 200 mm
    K = (Lc * 0.000306 + -0.4498) * param["Cf"] # stroke 200 mm
    tc = K * (Rc.apply(rstt) - rstt(Ri))

    #print( "Sensor: {}, dlp: {}".format(sensor, dlp.head()) )

    return dll+tc, dlp+tc, Rc.apply(rstt) 

    


def read_extention_meter(station, sensor, start=None, end=None, base_path="./extentiometer-data", fname=None, fend=".dat", logging_level=logging.WARNING):
    """
                date time, Channel, MuxChan, BeginFreq, EndFreq, ExciteVolts, 
        yr-mn-dy hr:mi:sc, ch,          mxc,       bfr,     efr,         exv,
              
              Freq_Value, Peak Amplitude, Signal-Noise Ratio, Noise Freq, Decay Ratio, Therm Resistance
                      Fc,            pka,                snr,        nfr,         dcr,               Rc
    """
    
    import os
    import logging

    import pandas as pd
    from datetime import datetime as dt
    from datetime import timedelta

    import gtimes.timefunc as gt


    # Handling logging
    logging.basicConfig(
        format="%(asctime)-15s [%(levelname)s] %(funcName)s: %(message)s",
        level=logging_level )
    logging.getLogger().setLevel(logging_level)
    module_logger = logging.getLogger()


    dstr="%Y%m%d %H:%M:%S"
    # defing default period to extract
    if end == None:
        end = dt.now()
    if start == None: # None we read the whole data set
        start = gt.toDatetime("20180801 00:00:00",dstr)
    if type(start) is int:
        start = gt.currDatetime(start,refday=end)

    param=calibration_param(sensor)
    # formatting the file and constructing a list
    if fname is None:
        fname = param["name"]

    fstring="{0}/%Y/{1}/{1}_{2}-%Y%m{3}".format(os.path.join(base_path),station,fname,fend)
    # time formating
    tstrf="%Y%m%d %H:%M:%S"
    
    flist=gt.datepathlist(fstring,'1M',start,end, closed=None )
    print(flist)

    data = pd.DataFrame()
    for dfile in flist:
        tmp = pd.DataFrame()
        
        try:
            tmp = pd.read_csv(dfile, index_col=0, delimiter ='\t', parse_dates=True)
        
        except  IOError as e:
            module_logger.warning("While reading File {}:  ".format(dfile) + str(e))
        except  pd.errors.EmptyDataError as e:
            module_logger.warning("While reading File {}:  ".format(dfile) + str(e))
        except BaseException as e:
            module_logger.error("While reading File {}:  ".format(dfile) + str(e))
            raise

        
        data = data.append(tmp)

    #path="./extentiometer-data/2019/svinafellsheidi/"
    #filename=path+"svinafellsheidi_shg2-201909.dat"
    #data = pd.read_csv(filename, index_col=0, delimiter ='\t', parse_dates=True) 
    #print(data["Freq_Value"])

    if len(data.index) > 0:
        data["dll"], data["dlp"], data["T"] = rstdl(sensor, data)
    
    return data


# End of extention stuff


def polar_plot(fig, thlim=(0, 180), rlim=(0, 1), step=(30, 0.2),
                          thlabel='theta', rlabel='r', ticklabels=True):
    """Return polar axes that adhere to desired theta (in deg) and r limits. steps for theta
    and r are really just hints for the locators. Using negative values for rlim causes
    problems for GridHelperCurveLinear for some reason"""
    
    from matplotlib.transforms import Affine2D
    import mpl_toolkits.axisartist.floating_axes as floating_axes
    import mpl_toolkits.axisartist.angle_helper as angle_helper
    from matplotlib.projections import PolarAxes

    from mpl_toolkits.axisartist.grid_finder import (FixedLocator, MaxNLocator, DictFormatter)


    th0, th1 = thlim # deg
    r0, r1 = rlim
    thstep, rstep = step

    # rotate a bit for better orientation
    tr_rotate = Affine2D().translate(-95, 0)

    # scale degrees to radians:
    tr_scale = Affine2D().scale(np.pi/180., 1.)
    
    tr = tr_scale + PolarAxes.PolarTransform()

    theta_grid_locator = angle_helper.LocatorDMS((th1-th0) // thstep)
    r_grid_locator = MaxNLocator((r1-r0) // rstep)
    theta_tick_formatter = angle_helper.FormatterDMS()
    grid_helper = GridHelperCurveLinear(tr,
                                        extremes=(th0, th1, r0, r1),
                                        grid_locator1=theta_grid_locator,
                                        grid_locator2=r_grid_locator,
                                        tick_formatter1=theta_tick_formatter,
                                        tick_formatter2=None)

    a = FloatingSubplot(f, 111, grid_helper=grid_helper)
    f.add_subplot(a)

    # adjust x axis (theta):
    a.axis["bottom"].set_visible(False)
    a.axis["top"].set_axis_direction("bottom") # tick direction
    a.axis["top"].toggle(ticklabels=ticklabels, label=bool(thlabel))
    a.axis["top"].major_ticklabels.set_axis_direction("top")
    a.axis["top"].label.set_axis_direction("top")

    # adjust y axis (r):
    a.axis["left"].set_axis_direction("bottom") # tick direction
    a.axis["right"].set_axis_direction("top") # tick direction
    a.axis["left"].toggle(ticklabels=ticklabels, label=bool(rlabel))

    # add labels:
    a.axis["top"].label.set_text(thlabel)
    a.axis["left"].label.set_text(rlabel)

    # create a parasite axes whose transData is theta, r:
    auxa = a.get_aux_axes(tr)
    # make aux_ax to have a clip path as in a?:
    auxa.patch = a.patch 
    # this has a side effect that the patch is drawn twice, and possibly over some other
    # artists. So, we decrease the zorder a bit to prevent this:
    a.patch.zorder = -2

    # add sector lines for both dimensions:
    thticks = grid_helper.grid_info['lon_info'][0]
    rticks = grid_helper.grid_info['lat_info'][0]
    for th in thticks[1:-1]: # all but the first and last
        auxa.plot([th, th], [r0, r1], '--', c='grey', zorder=-1)
    for ri, r in enumerate(rticks):
        # plot first r line as axes border in solid black only if it isn't at r=0
        if ri == 0 and r != 0:
            ls, lw, color = 'solid', 2, 'black'
        else:
            ls, lw, color = 'dashed', 1, 'grey'
        # From http://stackoverflow.com/a/19828753/2020363
        auxa.add_artist(plt.Circle([0, 0], radius=r, ls=ls, lw=lw, color=color, fill=False,
                        transform=auxa.transData._b, zorder=-1))
    return auxa


def line(x, p0, p1):
    return  p0 + p1*x


def fittimes(func, x, y, yD=None, p0=None):
    
    from scipy import optimize

    pb = []
    pcov = []

    pb, pcov = optimize.curve_fit(func, x, y, p0=p0, sigma=yD, maxfev=100000)

    return pb, pcov

def gpsbaseline(stat1,stat2):
    """
    """

def z2polar(z):
    """
    """
    from numpy import exp, abs, angle
    from numpy import abs, angle
    return  abs(z), angle(z) 


def polar2z(r,theta):
    """
    """

    from numpy import exp, asarray

    r=asarray(r)
    theta=asarray(theta)
 
    z = r * exp( 1j * theta )

    return z.real, z.imag

def pol2cart(r, theta):

    import numpy as np

    r=np.asarray(r)
    theta=np.asarray(theta)

    x = r * np.cos(theta)
    y = r * np.sin(theta)

    return x, y

def pi2minus(theta):

    from numpy import pi as pi

    return  [ pi/2 - x for x in theta ]

def plot_test():

    import numpy as np
    import matplotlib.dates as mdates    
    import pandas as pd

    from scipy import stats
    from matplotlib import style
    style.use('seaborn-whitegrid')

    import matplotlib.pyplot as plt
    from matplotlib import cm


    import timesmatplt.timesmatplt as tplt
    #import timesmatplt.gasmatplt as gplt
    from timesfunc.timesfunc import convGlobktopandas

    SVIN = convGlobktopandas(*tplt.getData("SVIN")[:3])
    SVIE = convGlobktopandas(*tplt.getData("SVIE")[:3])
    SKFC = convGlobktopandas(*tplt.getData("SKFC")[:3])
    SLEC = convGlobktopandas(*tplt.getData("SLEC")[:3])
     
    pi = np.pi
    thetadirection = -1
    thetaoffset = pi/2
    thetageodec=330
    thetageorad = np.deg2rad(thetageodec) + thetaoffset
    #thetarad=  -1 * thetadirection * (thetageorad - pi/2) 
    thetarad=  np.deg2rad(thetageodec)


    SVSV = (SVIN[['north','east','up']] - SVIE[['north','east','up']]).dropna()
    SVSV = SVSV.join(SVIN['yearf'],how='inner')

    SKSL = (SKFC[['north','east','up']] - SLEC[['north','east','up']]).dropna()

    SVSK = (SVIN[['north','east','up']] - SKFC[['north','east','up']]).dropna()
    SESK = (SVIE[['north','east','up']] - SKFC[['north','east','up']]).dropna()

    SVSL = (SVIN[['north','east','up']] - SLEC[['north','east','up']]).dropna()
    SESL = (SVIE[['north','east','up']] - SLEC[['north','east','up']]).dropna()

    SVSV['direction'] = SVSV.east*np.cos(thetarad) + SVSV.north*np.sin(thetarad) 
    SVSV['Dlength'] = np.sqrt(np.square(SVSV.direction) + np.square(SVSV.up))

    SVSL['direction'] = SVSL.east*np.cos(thetarad) + SVSL.north*np.sin(thetarad) 
    SVSL['Dlength'] = np.sqrt(np.square(SVSL.direction) + np.square(SVSL.up))

    SVSK['direction'] = SVSK.east*np.cos(thetarad) + SVSK.north*np.sin(thetarad) 
    SVSK['direction'] = SVSK.east*np.cos(thetarad) + SVSK.north*np.sin(thetarad) 
    SVSK['Dlength'] = np.sqrt(np.square(SVSK.direction) + np.square(SVSK.up))

    SESK['direction'] = SESK.east*np.cos(thetarad) + SESK.north*np.sin(thetarad) 
    SESK['Dlength'] = np.sqrt(np.square(SESK.direction) + np.square(SESK.up))

    SKSL['direction'] = SKSL.east*np.cos(thetarad) + SKSL.north*np.sin(thetarad) 
    SKSL['Dlength'] = np.sqrt(np.square(SKSL.direction) + np.square(SKSL.up))
    # ---------------------------
    SVSV = SVSV - SVSV.iloc[:10].mean()


    # extentiometer
    sensor1="WM1255"
    sensor2="WM1252"
    station="svinafellsheidi"
    shg1 = read_extention_meter(station, sensor1, start=None, end=None, base_path="./extentiometer-data", fname="shg1", fend=".dat")
    shg2 = read_extention_meter(station, sensor2, start=None, end=None, base_path="./extentiometer-data", fname="shg2", fend=".dat")


    #print(x)
    #print("TEST: {}".format(SVSV.Dlength))
    pb, plcov = fittimes(line, SVSV.yearf, SVSV.direction.values)
    pbl, plcovl = fittimes(line, SVSV.yearf, SVSV.Dlength.values)
    print(pb)
    #print(pl)

    SVSV['hlength'] = np.sqrt(np.square(SVSV[['east','north']]).sum(axis=1))
    #print(  np.angle(np.array(SVSV[['east','north']]) ) )
    SVSV['hangle'] =  thetadirection * ( np.arctan2(SVSV['north'], SVSV['east']) - thetaoffset )
    print(np.rad2deg(SVSV['hangle']).max())
    print(np.rad2deg(SVSV['hangle']).min())

    #print(np.angle([SVSV.east, SVSV.north]))

    SKSL = SKSL - SKSL[SVSV.index.min():].iloc[:10].mean()

    SVSK = SVSK - SVSK[SVSV.index.min():].iloc[:10].mean()
    SESK = SESK - SESK[SVSV.index.min():].iloc[:10].mean()

    SVSL = SVSL - SVSL[SVSV.index.min():].iloc[:10].mean()
    SESL = SESL - SESL[SVSV.index.min():].iloc[:10].mean()

    #fit line
    fit=line(SVSV.yearf,*pb)

    # ----- Plot stuff --------------------------
    fig = plt.figure(figsize=(10,10)) 
    fig.add_subplot(212)
    fig.axes[0].xaxis_date()
    fig.add_subplot(221,  polar=True)
    fig.add_subplot(222 )

    tax = fig.axes[0]
    pax = fig.axes[1]
    sax = fig.axes[2]
    pax.set_theta_offset(thetaoffset)
    pax.set_theta_direction(thetadirection)
    #pax.set_theta_zero_location("N")
    labels = ("N", "NE", "E", "SE", "S", "SW", "W", "NW", "N", "NE", "E", "SE", "S", "SW", "W","NW", "N")
    angles = (-360, -315, -270, -225, -180, -135, -90, -45, 0, 45, 90, 135, 180, 225, 270, 315, 360)
    pax.set_thetagrids(angles, labels=labels)
    #pax.set_xticklabels(labels)
    #pax.set_rlabel_position(np.rad2deg(3*pi/2-thetaoffset))
    #pax.set_rlabel_position(pi)
    #pax.set_ylim(0,5)
    pax.set_ylim(0,SVSV['hlength'].max()+5)
    pax.set_thetamin(-90)
    pax.set_thetamax(45)
    
    values = [0, SVSV['hlength'].max() ]
    values2 = [15, 15]
    angle1 = [thetarad , thetarad]
    angle2 = [thetarad - pi/2 , thetarad + pi/2]
    x, y = pol2cart(values, pi2minus(angle1)) 
    x2,y2  = pol2cart(values2, pi2minus(angle2)) 


    pax.plot(angle1, values, color="red", linewidth=1, linestyle='solid')
    pax.plot(angle2, values2, color="blue", linewidth=1, linestyle='solid', clip_on=False)

    #colors = cm.jet(np.linspace(0, 1, len(SVSV)))
    colors = cm.jet(np.linspace(0, 1, len(SVSV)))
    pax.scatter( SVSV['hangle'], SVSV['hlength'] , c=colors)
    
    #sax.set_ylim(0,5)
    #sax.set_xlim(-5,5)
    sax.scatter(SVSV['east'], SVSV['north'], c=colors )

    sax.plot(x,y , color="red", linewidth=1, linestyle='solid')
    sax.plot(x2, y2, color="blue", linewidth=1, linestyle='solid', clip_on=False)
    #SVSK.Dlength.plot(ax=tax, color='lightgrey')
    #SESK.Dlength.plot(ax=tax, color='lightgrey')
    #SKSL[SVSV.index.min():].Dlength.plot(ax=tax, color='lightgrey')

    #plotting   
    #fig = gplt.spltMultigasFrame()    
    #SVSL.direction.apply(lambda x: x*-1).plot(ax=tax, color='g')
    #SVSK.direction.apply(lambda x: x*-1).plot(ax=tax, color='b')
    #SVSL.east.plot(color='b')
    #SESL.east.plot(color='orange')
    #SVSV.direction.plot(ax=tax, color='b')

    zscore10 = stats.zscore( ( SVSV[['up','direction']] -  SVSV[['up','direction']].rolling(20, min_periods=1, center=False).median()) )
    #SVSV.up=SVSV.up[np.abs( zscore10[:,0] ) < 1]
    SVSV.direction=SVSV.direction[np.abs( zscore10[:,1] ) < 1]
    SVSV.up=SVSV.up[np.abs( zscore10[:,1] ) < 1]
    
    SVSV.direction.apply(lambda x: x*-1).plot(ax=tax, color='blue')
    #SVSV.up.apply(lambda x: x*-1).plot(ax=tax, color='yellow')
    shg1.dll.plot(ax=tax, color='green')
    shg2.dll.plot(ax=tax, color='red')
    #SVSV.up.plot(ax=tax, color='blue')
    #SVSV.Dlength.plot(ax=tax, color='r', ylim=(-3,15))
    #tax.plot(SVSV.index,fit-fit[0], linestyle='--', color='red', marker=None)

    #fig.axes[1].plot(111, projection='polar')

    fig.savefig("tmp.ps")
    # --------------------------------------


def main():
    """
    """

    import logging

    sensor1="WM1255"
    sensor2="WM1252"
    station="svinafellsheidi"
    plot_test()
    data = read_extention_meter(station, sensor2)
    print(data.tail())

if __name__=="__main__":
    main()
