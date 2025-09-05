#!/usr/bin/python3
# -*- coding: utf-8 -*-
from __future__ import print_function


def main():
    """
    """
    import pandas as pd
    import pandas_test as pdt
    
    # from matplotlib import style
    #style.use('seaborn-whitegrid')
    import matplotlib as mpl
    #mpl.use('ps')

    #import matplotlib.pyplot as plt
    #mpl.style.use('classic')

    #import matplotlib.dates as mdates

    import functions as ft
    sensors = ["WM1255", "WM1252", "WM1254"]
    #sensor = "WM1254"
    
    displacements = pd.DataFrame()
    legends=[]
    columns=['date', 'time', 'Freq_Value', 'T']
    for sensor in sensors:
        param=pdt.calibration_param(sensor)
    
        csv_file="./vw{}.csv".format(param["channel"])
        ndata = pd.read_csv(csv_file, sep=";", parse_dates=[['date', 'time']],  names=columns, index_col=[0], dayfirst=True)
        data = pdt.read_extention_meter('svinafellsheidi', sensor, base_path="/mnt/datadiskur/extension_meter/") 

    
        Li = param["Li"]
        #Rc = ndata["Therm Resistance"]
        Ri = param["Ri"]
        Lc = ndata["Freq_Value"]**2 / 1000
        dll = param["Cf"] * (Lc - Li) # breyting í mm, línuleg kvörðun
        dlp = (param["A"] * Lc**2 + param["B"] * Lc + param["C"]) \
                - (param["A"] * Li**2 + param["B"] * Li + param["C"])

        #K = (Lc * 0.000306 + -0.3186) * param["Cf"] # stroke 200 mm
        K = (Lc * 0.000306 + -0.4498) * param["Cf"] # stroke 200 mm

        #tc = K * (Rc.apply(rstt) - rstt(Ri))
        tc = K * (ndata["T"] - pdt.rstt(Ri))
        ndata["dll"]=dll+tc
        ndata = ndata["20191025":]
        print(ndata.index.min())
        #shg1[(shg1["dll"] < 5) and (shg1["dll"] > 0)
        data = pd.concat([data,ndata], axis=0) 
        newcolumn='dll_{}'.format(param['name'])
        data.rename( { 'dll': newcolumn }, axis='columns', inplace=True )

        displacements = displacements.join( data[newcolumn],  how='outer' )
        legends.append("Togmælir {} {}".format(param['name'],param['location'][0:2]))

    print( displacements.tail() ) 
    #fig = plt.figure(figsize=(10,5))

    #fig.add_subplot(111)
    #fig.axes[0].xaxis_date()
    #tax=fig.axes[0]
    #fig.autofmt_xdate()

    #tax.fmt_xdata = mdates.DateFormatter('%Y-%m-%d')
    start = displacements.index.min()
    end = displacements.index.max()
    

    Title="Togmælingar á Svínafellsheidi"
    #fig = ft.onesubFrame(Ylabel="Displacement [mm]", Title=Title)
    fig = ft.onesubFrame()
    fig = ft.onesubTickLabels(fig, period=(end-start))
    
    axes = fig.axes[0]
    axes.set_xlim([start,end])
    axes.set_ylabel("Færsla [mm]", fontsize='large', labelpad=4)
    axes.set_title(Title)

    #axes.plot_date(data.index, data.dll, marker='o', markersize=3.5, markerfacecolor='r', markeredgecolor='r')
    for column, legend  in zip(displacements.columns, legends):
        data=displacements[column].dropna()
        if column == "dll_shg3":
            data = data + 1.2
        axes.plot(data.index, data, label=legend) #, color="red" )
    
    axes.legend(loc="upper left", numpoints=2, frameon=False)
    axes.set_ylim(bottom=0)

    #data.dll.plot(ax=axes, color='green')
    #fig.savefig( "{}.eps".format(param["name"]), bbox_inches='tight' )
    #fig.savefig ("{}.pdf".format(param["name"], bbox='tight') )
    # fig.savefig( "{}.eps".format("Togmaelingar"), bbox_inches='tight')
    #fig.savefig( "{}.pdf".format("Togmaelingar"), bbox_inches='tight') 
    fig.savefig( "/home/gpsops/tmp/{}.png".format("Svinafellsheidi_togmaelingar"), bbox_inches='tight') 


if __name__ == '__main__':
    main()
