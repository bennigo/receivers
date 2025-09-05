#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

import numpy as np
import geofunc.geo as geo

def tstwofigTickLabels(fig,period=None):
    """
    """
    
    import matplotlib.pyplot as plt
    import matplotlib as mpl
    from datetime import timedelta
    import timesmatplt.timesmatplt as tplt
    import timesmatplt.gasmatplt as gplt

    #minorLoc, minorFmt, majorLoc,majorFmt = tsLabels(period)

    axes = fig.axes
    
    # major labels in separate layer
    axes[0].get_xaxis().set_tick_params(which='major', labelbottom='off') 
    axes[1].get_xaxis().set_tick_params(which='major', pad=15) 


    for ax in axes[-1:-4:-1]:
        #tickmarks lables and other and other axis stuff on each axes 
        # needs improvement
        ax.grid(True)
        #ax.grid(True,linestyle='solid',axis='x')
        if period < timedelta(2600):
            if ax is not axes[1]:
                ax.grid(True, which='minor',axis='x',)
                ax.grid(False, which='major',axis='x',)
    
        # --- X axis ---
        xax = ax.get_xaxis()


        xax = tplt.tslabels(xax,period=period,locator='minor',formater='minor')
        xax = tplt.tslabels(xax,period=period,locator='major',formater=None)
        if ax is axes[2]:
            xax = tplt.tslabels(xax,period=period,locator=None,formater='major')


        xax.set_tick_params(which='minor', direction='inout', length=4)
        xax.set_tick_params(which='major', direction='inout', length=15)

        for tick in xax.get_major_ticks():
            tick.label1.set_horizontalalignment('center')


        #for label in xax.get_ticklabels('major')[::]:
            #label.set_visible(False)
            #label.set_text("test")
         #   print "text: %s" % label.get_text()
            #xax.label.set_horizontalalignment('center')
    
        # --- Y axis ---
        yax = ax.get_yaxis()
        yax.set_minor_locator(mpl.ticker.AutoMinorLocator())
        yax.set_tick_params(which='minor', direction='in', length=4)
        yax.set_tick_params(which='major', direction='in', length=10)



    return fig

def tsTickLabels(fig,period=None):
    """
    """
    
    import matplotlib.pyplot as plt
    import matplotlib as mpl
    from datetime import timedelta
    import timesmatplt.timesmatplt as tplt
    import timesmatplt.gasmatplt as gplt

    #minorLoc, minorFmt, majorLoc,majorFmt = tsLabels(period)

    axes = fig.axes
    
    # major labels in separate layer
    axes[-1].get_xaxis().set_tick_params(which='major', pad=15) 
    axes[0].get_xaxis().set_tick_params(which='major', labelbottom='off') 
    axes[1].get_xaxis().set_tick_params(which='major', labelbottom='off') 


    for ax in axes[-1:-4:-1]:
        #tickmarks lables and other and other axis stuff on each axes 
        # needs improvement
        ax.grid(True)
        #ax.grid(True,linestyle='solid',axis='x')
        if period < timedelta(2600):
            ax.grid(True, which='minor',axis='x',)
            ax.grid(False, which='major',axis='x',)
    
        # --- X axis ---
        xax = ax.get_xaxis()

        xax = tplt.tslabels(xax,period=period,locator='minor',formater='minor')

        xax = tplt.tslabels(xax,period=period,locator='major',formater=None)
        if ax is axes[2]:
            xax = tplt.tslabels(xax,period=period,locator=None,formater='major')

        xax.set_tick_params(which='minor', direction='inout', length=4)
        xax.set_tick_params(which='major', direction='inout', length=15)

        for tick in xax.get_major_ticks():
            tick.label1.set_horizontalalignment('left')

        #for label in xax.get_ticklabels('major')[::]:
            #label.set_visible(False)
            #label.set_text("test")
         #   print "text: %s" % label.get_text()
            #xax.label.set_horizontalalignment('center')
    
        # --- Y axis ---
        yax = ax.get_yaxis()
        yax.set_minor_locator(mpl.ticker.AutoMinorLocator())
        yax.set_tick_params(which='minor', direction='in', length=4)
        yax.set_tick_params(which='major', direction='in', length=10)

    return fig

def getDetrFit(STA,onlyPeriodic=True):
    """
    """

    import numpy as np

    import timesmatplt.timesmatplt as tplt

    p0 = [[],[],[]]
    dtype=[('Nrate', '<f8'), ('Erate', '<f8'), ('Urate', '<f8'),
           ('Nacos', '<f8'), ('Nasin', '<f8'), ('Eacos', '<f8'),
           ('Easin', '<f8'), ('Uacos', '<f8'), ('Uasin', '<f8'),
           ('Nscos', '<f8'), ('Nssin', '<f8'), ('Escos', '<f8'),
           ('Essin', '<f8'), ('Uscos', '<f8'), ('Ussin', '<f8'),
           ('shortname', '|S5'), ('name', '|S20')]

    const = np.genfromtxt("itrf08det",dtype=dtype)
    const = [i for i in const if i[15] == STA]

    if const:
        if onlyPeriodic==True:
            const[0][0] = const[0][1] = const[0][2] = 0 

        p0[0] = [0, const[0][0], const[0][3], const[0][4], const[0][9], const[0][10] ] 
        p0[1] = [0, const[0][1], const[0][5], const[0][6], const[0][11], const[0][12] ]
        p0[2] = [0, const[0][2], const[0][7], const[0][8], const[0][13], const[0][14] ]
        p0[0] = [-1*i for i in p0[0] ]
        p0[1] = [-1*i for i in p0[1] ]
        p0[2] = [-1*i for i in p0[2] ]
    else:
        x, y, Dy = tplt.getData(STA,ref="plate")
        p0, _ = fittimes(lineperiodic, x, y, Dy)

    return p0



# Functions -----------------------------------
def line(x, p0, p1):
    return  p0 + p1*x

def lineperiodic(x, p0, p1, p2, p3, p4 ,p5):
    return p0 +  p1*x + p2*np.cos(2*np.pi*x) + p3*np.sin(2*np.pi*x) + p4*np.cos(4*np.pi*x) + p5*np.sin(4*np.pi*x) 

def xf(x, p0, p1, p2 ):

    import numpy as np

    tau = 4.80

    return p0 + p1 * x  + p2 * np.exp( - tau * x )

def expxf(x, p0, p1, p2, p3 ):

    import numpy as np

    return p0 + p1 * x  + p2 * np.exp( -p3* x )  

def expf(x, p0, p1, p2 ):

    import numpy as np

    return p0 + p1 * np.exp( -p2* x )  


def periodic(x, p0, p1, p2, p3, p4 ,p5):
    return   p2*np.cos(2*np.pi*x) + p3*np.sin(2*np.pi*x) + p4*np.cos(4*np.pi*x) + p5*np.sin(4*np.pi*x) 
# -------------------------------------------------

def fittimes(func, x, y, yD=[None,None,None], p0=[None,None,None]):
    
    from scipy import optimize

    pb = [[],[],[]]
    pcov = [[],[],[]]

    for i in range(3):
        pb[i], pcov[i] = optimize.curve_fit(func, x, y[i], p0=p0[i], sigma=yD[i], maxfev=100000)

    return pb, pcov


def detrend(x, y, Dy=None, fitfunc=lineperiodic, p=None, pcov=None, STA=None, onlyPeriodic=True, zref=False ):
    """
    Returning detrend parameters very preliminary construction
    """

    import numpy as np
    
    import timesfunc.timesfunc as gtf

    if Dy is None:
        Dy = np.ones(y.shape)


    # Handling parameters
    if p: # Parameters passed as arguments passed  
        pass
    else: 
        if STA:
            p0 = getDetrFit(STA,onlyPeriodic=onlyPeriodic)
        else:
            p0=[None,None,None]

        p, pcov = fittimes(fitfunc, x, y, Dy, p0=p0)

    for i in range(3):
        y[i] = y[i] - fitfunc(x,*p[i])
    
    if zref:
        _, y, _ = gtf.vshift(x, y, Dy, uncert=20.0, refdate=None, Period=5)

    return y


def xyzDict():
    """
    Extract xyz goordinate dictionary from global coordinate file defined by cparser
    """

    import cparser as cp

    xyzDict = {}

    f = open(cp.Parser().getPostprocessConfig()['coordfile'],'r')
    xyzDict.update( dict( [ [ line.split()[3], map(float,line.split()[0:3]) ] for line in f ] ) )

    return xyzDict

def pvel(pl,pcov):
    """
    """
    vunc = [None,None,None]
    vel = [None,None,None]

    for i in range(3):

        vunc[i] = np.sqrt(np.diag(pcov[i]))[1]
        vel[i] = pl[i][1]
        #print("{0:0.2f} {1:0.2f}".format( pl[i][1],vunc[i]) ) 

    return vel, vunc

def fit_exp_linear(t, y, C=0):
    y = y - C
    y = np.log(y)
    K, A_log = np.polyfit(t, y, 1)
    A = np.exp(A_log)
    return A, K


def llh(sta, radians=False, reference=geo.itrf2008):
    """
    convert xyz coordinates of a GPS station (assuming ITRF2008) llh coordinates
    """

    import pyproj as proj
    import geofunc.geo as geo

    #return proj.transform(geo.itrf2008, geo.lonlat, *xyzDict()[sta] , radians=radians)
    return proj.transform(reference, geo.lonlat, *xyzDict()[sta] , radians=radians)

def spltbbtwoFrame(Ylabel=None, Title=None):
    """
        Ylabel, 
        Title,

    output:
        fig, Figure object.

    """
    #import matplotlib.image as image

    from gtimes.timefunc import currTime
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    mpl.style.use('classic')

    
    # constructing a figure with three axes and adding a title
    fig, axes = plt.subplots(nrows=2, ncols=1, figsize=(13,7))
    fig.subplots_adjust(hspace=0.0)

    
    if type(Title) is list:
        plt.suptitle(Title[0], y=0.965,x=0.51)
        axes[0].set_title(Title[1],y=1.01)
    elif type(Title) is str:
        axes[0].set_title(Title)
    else:
        pass


    if Ylabel == None: #
        Ylabels = ("Y1","Y2","Y3")


    # --- apply custom stuff to the whole fig ---
    plt.minorticks_on
    plt.gcf().autofmt_xdate(rotation=0, ha='left')
    mpl.rcParams['legend.handlelength'] = 0    
    mpl.rcParams['text.usetex'] = True
    mpl.rc('text.latex', preamble=r'\usepackage{color}')
    
    for i in range(2):
        axes[i].set_ylabel(Ylabel, fontsize='x-large', labelpad=0)
        axes[i].axhline(0,color='black') # zero line

    for ax in axes[-1:-3:-1]:
    #tickmarks lables and other and other axis stuff on each axes 
    # needs improvementimport matplotlib.pyplot as plt
    
    # --- X axis ---

        xax = ax.get_xaxis()
    
        xax.set_tick_params(which='minor', direction='inout', length=4, top='off')
        xax.set_tick_params(which='major', direction='inout', length=10, top='off')

        if ax is axes[0]: # special case of top axes
            xax.set_tick_params(which='major', direction='inout', length=10, top='on')
            xax.set_tick_params(which='minor', direction='inout', length=4, top='on')
        else:
            ax.spines['top'].set_visible(False)


    return fig


def spltMultigasFrame(Ylabel=None, Title=None):
    """
        Ylabel, 
        Title,

    output:
        fig, Figure object.

    """
    #import matplotlib.image as image

    from gtimes.timefunc import currTime
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    mpl.style.use('classic')

    
    # constructing a figure with three axes and adding a title
    fig, ax = plt.subplots(nrows=1, ncols=1, figsize=(13,5))
    #fig.subplots_adjust(hspace=0.0)

    
    if type(Title) is list:
        plt.suptitle(Title[0], y=0.965,x=0.51)
        axes[0].set_title(Title[1],y=1.01)
    elif type(Title) is str:
        axes[0].set_title(Title)
    else:
        pass


    if Ylabel == None: #
        Ylabels = ("Y1","Y2","Y3")


    # --- apply custom stuff to the whole fig ---
    plt.minorticks_on
    plt.gcf().autofmt_xdate(rotation=0, ha='left')
    mpl.rcParams['legend.handlelength'] = 0    
    mpl.rcParams['text.usetex'] = True
    mpl.rc('text.latex', preamble=r'\usepackage{color}')
    
    ax.set_ylabel(Ylabel, fontsize='x-large', labelpad=0)
    ax.axhline(0,color='black') # zero line


    #tickmarks lables and other and other axis stuff on each axes 
    # needs improvementimport matplotlib.pyplot as plt
    
    # --- X axis ---
    xax = ax.get_xaxis()
    
    xax.set_tick_params(which='minor', direction='inout', length=4, top='off')
    xax.set_tick_params(which='major', direction='inout', length=10, top='off')
        
    return fig


def onesubFrame(Ylabel=None, Title=None):
    """
    Frame for plotting standard GPS time series. takes in Ylabel and Title
    and constructs empty figuranglee with no data.
    input:
        Ylabel, 
        Title,

    output:
        fig, Figure object.

    """
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    mpl.style.use('classic')
    #import matplotlib.image as image

    from gtimes.timefunc import currTime
    
    # constructing a figure with three axes and adding a title
    fig, axes = plt.subplots(nrows=1, ncols=1, figsize=(13,6))
    fig.subplots_adjust(hspace=.1)

    if type(Title) is list:
        plt.suptitle(Title[0], y=0.935,x=0.51)
        axes.set_title(Title[1],y=1.01)
    elif type(Title) is str:
        axes.set_title(Title)
    else:
        pass

    if Ylabel == None: #
        Ylabel =  "[mm]"
        #Ylabel = ("Nordur [mm]","Austur [mm]","Upp [mm]")

    # --- apply custom stuff to the whole fig ---
    plt.minorticks_on
    plt.gcf().autofmt_xdate(rotation=0, ha='left')
    mpl.rcParams['legend.handlelength'] = 0    
    #mpl.rcParams['text.usetex'] = True
    #mpl.rcParams['text.latex.unicode'] = True
    #mpl.rc('text.latex', preamble=r"\usepackage{color}")
    #mpl.rc('text.latex', preamble='\usepackage[pdftex]{graphicx}')
    #mpl.rc('text.latex', preamble='\usepackage[icelandic]{babel}')
    #mpl.rc('text.latex', preamble='\usepackage[T1]{fontenc}')
    #mpl.rc('font', family='Times New Roman')
    
    axes.set_ylabel(Ylabel, fontsize='x-large', labelpad=0)
    axes.axhline(0,color='black') # zero line


    #tickmarks lables and other and other axis stuff on each axes 
    # --- X axis ---
    xax = axes.get_xaxis()
    
    xax.set_tick_params(which='minor', direction='inout', length=4, top='off')
    xax.set_tick_params(which='major', direction='inout', length=10, top='off')
        
    xax.set_tick_params(which='major', direction='inout', length=10, top='on')
    xax.set_tick_params(which='minor', direction='inout', length=4, top='on')
            
    return fig

def onesubTickLabels(fig,period=None):
    """
    """
    
    import matplotlib.pyplot as plt
    import matplotlib as mpl
    from datetime import timedelta
    
    import timesmatplt.timesmatplt as tplt
    #minorLoc, minorFmt, majorLoc,majorFmt = tsLabels(period)

    axes = fig.axes
    
    # major labels in separate layer
    axes[0].get_xaxis().set_tick_params(which='major', pad=15) 
    #axes[0].get_xaxis().set_tick_params(which='major', labelbottom='off') 
    #axes[1].get_xaxis().set_tick_params(which='major', labelbottom='off') 


    for ax in axes:
        #tickmarks lables and other and other axis stuff on each axes 
        # needs improvement
        ax.grid(True)
        #ax.grid(True,linestyle='solid',axis='x')
        if period < timedelta(2600):
            ax.grid(True, which='minor',axis='x',)
            ax.grid(False, which='major',axis='x',)
    
        # --- X axis ---
        xax = ax.get_xaxis()

        xax = tplt.tslabels(xax,period=period,locator='minor',formater='minor')

        xax = tplt.tslabels(xax,period=period,locator='major',formater=None)
        xax = tplt.tslabels(xax,period=period,locator=None,formater='major')

        xax.set_tick_params(which='minor', direction='inout', length=4)
        xax.set_tick_params(which='major', direction='inout', length=15)

        for tick in xax.get_major_ticks():
            tick.label1.set_horizontalalignment('left')

        #for label in xax.get_ticklabels('major')[::]:
            #label.set_visible(False)
            #label.set_text("test")
         #   print "text: %s" % label.get_text()
            #xax.label.set_horizontalalignment('center')
    
        # --- Y axis ---
        yax = ax.get_yaxis()
        yax.set_minor_locator(mpl.ticker.AutoMinorLocator())
        yax.set_tick_params(which='minor', direction='in', length=4)
        yax.set_tick_params(which='major', direction='in', length=10)


    return fig


def plotSingMultigas(data, aquisitions=None, gastypes=None, colors=None, warnings=None, ylims=None, start=None, end=None):
    """
    """
    import matplotlib.pyplot as plt


    fig = spltMultigasFrame()


    topax = fig.axes[0] 
    lowax = fig.axes[1] 
    topmaxes = [topax, topax.twinx(), topax.twinx()]
    fig.subplots_adjust(right=0.75)

    lowax.set_xlim([start,end])
    fig = tsSingTickLabels(fig, period=(end-start))

    maxes[-1].spines['right'].set_position(('axes', 1.08))
    maxes[-1].set_frame_on(True)
    maxes[-1].patch.set_visible(False)

    for axs, color, gastype, warning, ylim in zip(maxes, colors, gastypes, warnings, ylims): 
        if len(ylim) == 1:
            ymin = gdata[gastype].min() 
            ymax = gdata[gastype].max()
            axs.set_ylim([ymin-ylim[0],ymax+ylim[0]])
        elif len(ylim) ==2:
            axs.set_ylim([ylim[0],ylim[1]])


        for aq in aquisitions:
            pdata=data[data.aquisition == aq ][1:]
            x = pdata.index
            y = pdata[gastype]
            # --- Plotting the data
    
            axs.plot(x,y,linewidth=2,color=color)
            axs.axhline(y=warning, linewidth=2, color = color)
        
        axs.set_ylabel('%s' % gastype, color=color)
        axs.tick_params(axis='y', colors=color)
 
    return fig

