import numpy as np
import astropy.io.fits as pf
from astropy.io import ascii
from scipy.interpolate import interp1d, griddata, NearestNDInterpolator
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter as FSF
import h5py
import multiprocessing as mp
from numpy.fft import fft, ifft, fftfreq, fftshift, ifftshift
from scipy.interpolate import InterpolatedUnivariateSpline
from scipy.integrate import trapz,simps
import pyfftw

c_kms = 2.99792458e5 #km s^-1
wl_file = pf.open("WAVE_PHOENIX-ACES-AGSS-COND-2011.fits")
w_full = wl_file[0].data
wl_file.close()
ind = (w_full > 3000.) & (w_full < 13000.) #this corresponds to some extra space around the
# shortest U and longest z band
global w
w = w_full[ind]
len_p = len(w)

wave_grid_fine = np.load('wave_grid_0.35kms.npy')
wave_grid_coarse = np.load('wave_grid_2kms.npy')

L_sun = 3.839e33 #erg/s, PHOENIX header says W, but is really erg/s
R_sun = 6.955e10 #cm

F_sun = L_sun/(4 * np.pi * R_sun**2) #bolometric flux of the Sun measured at the surface
print(F_sun)

Ts = np.arange(2300, 12001, 100)
loggs = np.arange(0.0, 6.1, 0.5)


def write_Ts_loggs():
    T_list = []
    logg_list = []
    for T in Ts:
        for logg in loggs:
            T_list.append(T)
            logg_list.append(logg)

    ascii.write({"T": T_list, "logg": logg_list}, "param_grid.txt", names=["T", "logg"])


T_points = np.array(
    [2300, 2400, 2500, 2600, 2700, 2800, 2900, 3000, 3100, 3200, 3300, 3400, 3500, 3600, 3700, 3800, 4000, 4100, 4200,
     4300, 4400, 4500, 4600, 4700, 4800, 4900, 5000, 5100, 5200, 5300, 5400, 5500, 5600, 5700, 5800, 5900, 6000, 6100,
     6200, 6300, 6400, 6500, 6600, 6700, 6800, 6900, 7000, 7200, 7400, 7600, 7800, 8000, 8200, 8400, 8600, 8800, 9000,
     9200, 9400, 9600, 9800, 10000, 10200, 10400, 10600, 10800, 11000, 11200, 11400, 11600, 11800, 12000])
logg_points = np.arange(0.0, 6.1, 0.5)
Z_points = ['-0.5','-0.0','+0.5']

#shorten for ease of use
#T_points = T_points[16:-25]
#logg_points = logg_points[2:-2]

print("T_points", T_points)
print("logg_points", logg_points)
print("Z_points", Z_points)

def create_wave_grid(v=1., start=3700., end=10000):
    '''Returns a grid evenly spaced in velocity'''
    size = 9000000 #this number just has to be bigger than the final array
    lam_grid = np.zeros((size,))
    i = 0
    lam_grid[i] = start
    vel = np.sqrt((c_kms + v) / (c_kms - v))
    while (lam_grid[i] < end) and (i < size - 1):
        lam_new = lam_grid[i] * vel
        i += 1
        lam_grid[i] = lam_new
    return lam_grid[np.nonzero(lam_grid)][:-1]

def create_fine_and_coarse_wave_grid():
    wave_grid_coarse = create_wave_grid(2., start=3050., end=11322.2) #chosen for 3 * 2**16 = 196608
    wave_grid_fine = create_wave_grid(0.35, start=3050., end=12089.65) # chosen for 9 * 2 **17 = 1179648

    np.save('wave_grid_2kms.npy',wave_grid_coarse)
    np.save('wave_grid_0.35kms.npy',wave_grid_fine)
    print(len(wave_grid_coarse))
    print(len(wave_grid_fine))


def point_resolver():
    '''Resolves a continous query in temp, logg to the nearest parameter combo in the PHOENIX grid. All available
    combinations are listed in param_grid.txt.'''
    points = np.loadtxt("param_grid_GWOri.txt")
    pr = NearestNDInterpolator(points, points) #Called as pr(5713, 3.45)
    return pr


def write_hdf5():
    '''create an hdf5 file of the PHOENIX grid. Go through each T point, if the corresponding logg exists,
    write it. If not, write zeros.'''
    f = h5py.File("LIB.hdf5", "w")
    shape = (len(T_points), len(logg_points), len_p)
    dset = f.create_dataset("LIB", shape, dtype="f")
    for t, temp in enumerate(T_points):
        for l, logg in enumerate(logg_points):
            try:
                flux = load_flux_npy(temp, logg)
                print("Wrote %s, %s" % (temp, logg))
            except OSError:
                print("%s, %s does not exist!" % (temp, logg))
                flux = np.nan
            dset[t, l, :] = flux


def flux_interpolator():
    points = ascii.read("param_grid_GWOri.txt")
    T_list = points["T"].data
    logg_list = points["logg"].data
    fluxes = np.empty((len(T_list), len(w)))
    for i in range(len(T_list)):
        fluxes[i] = load_flux_npy(T_list[i], logg_list[i])
    flux_intp = NearestNDInterpolator(np.array([T_list, logg_list]).T, fluxes)
    return flux_intp


def flux_interpolator_np():
    points = np.loadtxt("param_grid_GWOri.txt")
    print(points)
    #T_list = points["T"].data
    #logg_list = points["logg"].data
    len_w = 716665
    fluxes = np.empty((len(points), len_w))
    for i in range(len(points)):
        fluxes[i] = load_flux_npy(points[i][0], points[i][1])
    flux_intp = NearestNDInterpolator(points, fluxes)
    return flux_intp


def rewrite_flux(temp, logg):
    rname = "HiResFITS/PHOENIX-ACES-AGSS-COND-2011/Z-0.0/lte{temp:0>5.0f}-{logg:.2f}-0.0" \
            ".PHOENIX-ACES-AGSS-COND-2011-HiRes.fits".format(
        temp=temp, logg=logg)

    wname = "HiResNpy/PHOENIX-ACES-AGSS-COND-2011/Z-0.0/lte{temp:0>5.0f}-{logg:.2f}-0.0" \
            ".PHOENIX-ACES-AGSS-COND-2011-HiRes.npy".format(
        temp=temp, logg=logg)

    try:
        flux_file = pf.open(rname)
        f = flux_file[0].data
        flux_file.close()
        f = f[ind]
        print("Loaded " + rname)
        print(f.dtype)
        np.save(wname, f)
        print("Wrote " + wname)
        print()
    except OSError:
        print(rname + " does not exist!")


def rewrite_wl():
    np.save("ind.npy", ind)
    np.save("wave_trim.npy", w)


def load_flux_npy(temp, logg):
    rname = "HiResNpy/PHOENIX-ACES-AGSS-COND-2011/Z-0.0/lte{temp:0>5.0f}-{logg:.2f}-0.0" \
            ".PHOENIX-ACES-AGSS-COND-2011-HiRes.npy".format(
        temp=temp, logg=logg)
    print("Loading " + rname)
    return np.load(rname)


def load_flux_fits(temp, logg):
    rname = "HiResFITS/PHOENIX-ACES-AGSS-COND-2011/Z-0.0/lte{temp:0>5.0f}-{logg:.2f}-0.0" \
            ".PHOENIX-ACES-AGSS-COND-2011-HiRes.fits".format(
        temp=temp, logg=logg)
    flux_file = pf.open(rname)
    f = flux_file[0].data
    flux_file.close()
    f = f[ind]
    print("Loaded " + rname)
    #Print Radius and Temperature
    return f


def load_flux_full(temp, logg, Z, norm=False):
    '''Load a raw PHOENIX spectrum based upon temp, logg, and Z. '''

    rname = "HiResFITS/PHOENIX-ACES-AGSS-COND-2011/Z{Z:}/lte{temp:0>5.0f}-{logg:.2f}{Z:}" \
            ".PHOENIX-ACES-AGSS-COND-2011-HiRes.fits".format(Z=Z, temp=temp, logg=logg)

    flux_file = pf.open(rname)
    f = flux_file[0].data
    #L = flux_file[0].header['PHXLUM'] #erg/s

    if norm:
        f *= 1e-8
        F_bol = trapz(f, w_full)
        f = f * (F_sun / F_bol)
        print("Normalized %s, %s, %s, bolometric flux to 1 F_sun" % (temp, logg, Z))
        #this also means that the bolometric luminosity is always 1 L_sun
    flux_file.close()
    print("Loaded " + rname)
    return f

def load_flux_full_Z0(temp, logg, norm=False):
    rname = "HiResFITS/PHOENIX-ACES-AGSS-COND-2011/Z-0.0/lte{temp:0>5.0f}-{logg:.2f}-0.0" \
            ".PHOENIX-ACES-AGSS-COND-2011-HiRes.fits".format(
        temp=temp, logg=logg)
    flux_file = pf.open(rname)
    f = flux_file[0].data
    L = flux_file[0].header['PHXLUM'] #W
    if norm:
        f = f * (L_sun / L)
        print("Normalized luminosity to 1 L_sun")
    flux_file.close()
    print("Loaded " + rname)
    return f

@np.vectorize
def gauss_taper(s, sigma=2.89):
    '''This is the FT of a gaussian w/ this sigma. Sigma in km/s'''
    return np.exp(-2 * np.pi ** 2 * sigma ** 2 * s ** 2)

def resample_and_convolve(f, wg_fine, wg_coarse, wg_fine_d=0.35, sigma=2.89):
    '''Take a full-resolution PHOENIX spectrum `f`, resample it to a fine wave_grid,
    instrumentally broaden it, then resample it to a coarser wave_grid. sigma in km/s.'''

    #resample PHOENIX to 0.35km/s spaced grid using InterpolatedUnivariateSpline
    interp_P = InterpolatedUnivariateSpline(w,f)
    f_grid = interp_P(wg_fine)

    #Fourier Transform
    out = fft(f_grid)
    #The frequencies (cycles/km) corresponding to each point
    freqs = fftfreq(len(f_grid), d=wg_fine_d)

    #Instrumentally broaden the spectrum by multiplying with a Gaussian in Fourier space (corresponding to FWHM 6.8km/s)
    taper = np.exp(-2 * (np.pi ** 2) * (sigma ** 2) * (freqs ** 2))
    tout = out * taper

    #Take the broadened spectrum back to wavelength space
    f_grid6 = ifft(tout)
    print("Total of imaginary components", np.sum(np.abs(np.imag(f_grid6))))

    #Resample the broadened spectrum to a uniform coarse grid
    interp_6 = InterpolatedUnivariateSpline(wg_fine,np.abs(f_grid6))
    f_grid6_coarse = interp_6(wg_coarse)

    return f_grid6_coarse

def create_grid_parallel(ncores):
    '''create an hdf5 file of the PHOENIX grid. Go through each T point, if the corresponding logg exists,
    write it. If not, write nan. Each spectrum is normalized to the bolometric flux at the surface of the Sun.'''
    f = h5py.File("LIB_2kms.hdf5", "w")
    shape = (len(T_points), len(logg_points), len(Z_points), len(wave_grid_coarse))
    dset = f.create_dataset("LIB", shape, dtype="f")

    # A thread pool of P processes
    pool = mp.Pool(ncores)

    param_combos = []
    var_combos = []
    for t, temp in enumerate(T_points):
        for l, logg in enumerate(logg_points):
            for z, Z in enumerate(Z_points):
                param_combos.append([t, l, z])
                var_combos.append([temp, logg, Z])

    spec_gen = list(pool.map(process_spectrum, var_combos))
    for i in range(len(param_combos)):
        t, l, z = param_combos[i]
        dset[t, l, z, :] = spec_gen[i]

    f.close()

def create_grid_parallel_Z0(ncores):
    '''create an hdf5 file of the PHOENIX grid. Go through each T point, if the corresponding logg exists,
    write it. If not, write nan.'''
    f = h5py.File("LIB_2kms.hdf5", "w")
    shape = (len(T_points), len(logg_points), len(wave_grid_coarse))
    dset = f.create_dataset("LIB", shape, dtype="f")

    # A thread pool of P processes
    pool = mp.Pool(ncores)

    param_combos = []
    var_combos = []
    for t, temp in enumerate(T_points):
        for l, logg in enumerate(logg_points):
            param_combos.append([t, l])
            var_combos.append([temp, logg])

    spec_gen = list(pool.map(process_spectrum_Z0, var_combos))
    for i in range(len(param_combos)):
        t, l = param_combos[i]
        dset[t, l, :] = spec_gen[i]

    f.close()

def process_spectrum(pars):
    temp, logg, Z = pars
    try:
        f = load_flux_full(temp, logg, Z, True)[ind]
        flux = resample_and_convolve(f,wave_grid_fine,wave_grid_coarse)
        print("Finished %s, %s, %s" % (temp, logg, Z))
    except OSError:
        print("%s, %s, %s does not exist!" % (temp, logg, Z))
        flux = np.nan
    return flux

def process_spectrum_Z0(pars):
    temp, logg = pars
    try:
        f = load_flux_full(temp, logg, True)[ind]
        flux = resample_and_convolve(f,wave_grid_fine,wave_grid_coarse)
        print("Finished %s, %s" % (temp, logg))
    except OSError:
        print("%s, %s does not exist!" % (temp, logg))
        flux = np.nan
    return flux




# Interpolation routines
def interpolate_raw_test_temp():
    base = 'data/LkCa15//LkCa15_2013-10-13_09h37m31s_cb.flux.spec.'
    wls = np.load(base + "wls.npy")
    fls = np.load(base + "fls.npy")
    wl = wls[22]
    ind2 = (m.w_full > wl[0]) & (m.w_full < wl[-1])
    w = m.w_full[ind2]
    f58 = load_flux_npy(5800, 3.5)[ind2]
    f59 = load_flux_npy(5900, 3.5)[ind2]
    f60 = load_flux_npy(6000, 3.5)[ind2]

    bit = np.array([5800, 6000])
    f = np.array([f58, f60]).T
    func = interp1d(bit, f)
    f59i = func(5900)

    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(111)
    ax.axhline(0, color="k")
    ax.plot(w, (f59 - f59i) * 100 / f59)
    ax.set_xlabel(r"$\lambda\quad[\AA]$")
    ax.xaxis.set_major_formatter(FSF("%.0f"))
    ax.set_ylabel("Fractional Error [\%]")
    fig.savefig("plots/interp_tests/5800_5900_6000_logg3.5.png")


def interpolate_raw_test_logg():
    base = 'data/LkCa15//LkCa15_2013-10-13_09h37m31s_cb.flux.spec.'
    wls = np.load(base + "wls.npy")
    fls = np.load(base + "fls.npy")

    wl = wls[22]
    ind2 = (m.w_full > wl[0]) & (m.w_full < wl[-1])
    w = m.w_full[ind2]
    f3 = load_flux_npy(5900, 3.0)[ind2]
    f3_5 = load_flux_npy(5900, 3.5)[ind2]
    f4 = load_flux_npy(5900, 4.0)[ind2]

    bit = np.array([3.0, 4.0])
    f = np.array([f3, f4]).T
    func = interp1d(bit, f)
    f3_5i = func(3.5)

    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(111)
    ax.axhline(0, color="k")
    ax.plot(w, (f3_5 - f3_5i) * 100 / f3_5)
    ax.set_xlabel(r"$\lambda\quad[\AA]$")
    ax.xaxis.set_major_formatter(FSF("%.0f"))
    ax.set_ylabel("Fractional Error [\%]")
    fig.savefig("plots/interp_tests/5900_logg3_3.5_4.png")


def interpolate_test_temp():
    base = 'data/LkCa15//LkCa15_2013-10-13_09h37m31s_cb.flux.spec.'
    wls = np.load(base + "wls.npy")
    fls = np.load(base + "fls.npy")

    f58 = load_flux_npy(2400, 3.5)
    f59 = load_flux_npy(2500, 3.5)
    f60 = load_flux_npy(2600, 3.5)
    bit = np.array([2400, 2600])
    f = np.array([f58, f60]).T
    func = interp1d(bit, f)
    f59i = func(2500)

    d59 = m.degrade_flux(wl, m.w_full, f59)
    d59i = m.degrade_flux(wl, m.w_full, f59i)

    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(111)
    ax.axhline(0, color="k")
    ax.plot(wl, (d59 - d59i) * 100 / d59)
    ax.set_xlabel(r"$\lambda\quad[\AA]$")
    ax.xaxis.set_major_formatter(FSF("%.0f"))
    ax.set_ylabel("Fractional Error [\%]")
    fig.savefig("plots/interp_tests/2400_2500_2600_logg3.5_degrade.png")


def interpolate_test_logg():
    base = 'data/LkCa15//LkCa15_2013-10-13_09h37m31s_cb.flux.spec.'
    wls = np.load(base + "wls.npy")
    fls = np.load(base + "fls.npy")

    wl = wls[22]

    f3 = load_flux_npy(2400, 3.0)
    f3_5 = load_flux_npy(2500, 3.5)
    f4 = load_flux_npy(2600, 4.0)

    bit = np.array([3.0, 4.0])
    f = np.array([f3, f4]).T
    func = interp1d(bit, f)
    f3_5i = func(3.5)

    d3_5 = m.degrade_flux(wl, m.w_full, f3_5)
    d3_5i = m.degrade_flux(wl, m.w_full, f3_5i)

    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(111)
    ax.axhline(0, color="k")
    ax.plot(wl, (d3_5 - d3_5i) * 100 / d3_5)
    ax.set_xlabel(r"$\lambda\quad[\AA]$")
    ax.xaxis.set_major_formatter(FSF("%.0f"))
    ax.set_ylabel("Fractional Error [\%]")
    fig.savefig("plots/interp_tests/2500logg3_3.5_4_degrade.png")

def compare_PHOENIX_TRES_spacing():
    fig = plt.figure(figsize=(8, 4))
    ax = fig.add_subplot(111)
    wave_TRES = trunc_tres()
    #index as percentage of full grid
    #ind = (wave_grid > wave_TRES[0]) & (wave_grid < wave_TRES[-1])
    #w_grid = wave_grid[ind]
    #w_pixels = np.arange(0,len(w_grid),1)/len(w_grid)
    #ax.plot(w_grid[:-1], v(w_grid[:-1],w_grid[1:]),label="Constant V")

    #t_pixels = np.arange(0,len(wave_TRES),1)/len(wave_TRES)
    #linear = np.linspace(wave_TRES[0],wave_TRES[-1])
    #l_pixels = np.arange(0,len(linear),1)/len(linear)

    ax.plot(wave_TRES[:-1], v(wave_TRES[:-1], wave_TRES[1:]), "g", label="TRES")
    ax.axhline(2.5)
    #ax.plot(linear[:-1], v(linear[:-1],linear[1:]),label="Linear")

    ax.set_xlabel(r"$\lambda$ [\AA]")
    ax.set_ylabel(r"$\Delta v$ [km/s]")
    ax.legend(loc='best')
    ax.set_ylim(2.2, 2.8)
    #plt.show()
    fig.savefig("plots/pixel_spacing_v.png")

@np.vectorize
def v(ls,lo):
    return c_kms * (lo ** 2 - ls ** 2) / (ls ** 2 + lo ** 2)

def trunc_tres():
    wl = wls[0]
    for i in range(1, 51):
        ind = (wls[i] > wl[-1])
        wl = np.append(wl, wls[i][ind])
    return wl


def PHOENIX_5000(temp, logg):
    '''Interpolate the entire PHOENIX spectrum to 0.06 spacing in anticipation of sinc interpolation.'''
    global w
    f_full = load_flux_full(temp, logg, True)[ind]
    #take all points longer than 5000 ang
    ind3 = (w < 5000)
    f_3 = f_full[ind3]
    w_3 = w[ind3]
    ind5 = (w >= 5000) & (w < 10000)
    f_5 = f_full[ind5]
    w_5 = w[ind5]
    ind10 = (w >= 10000)
    f_10 = f_full[ind10]
    w_10 = w[ind10]

    #for 5000
    N = len(f_5)
    if N % 2 == 0:
        print("Even")
    else:
        print("Odd")

    out = fft(ifftshift(f_5))
    freqs = fftfreq(N, d=0.01) # spacing, Ang
    d = freqs[1]

    zeros_5 = np.zeros((333332,))

    w_50 = ifftshift(w_5)[0] #must do this to get the zeroth wavelength

    wout = ifftshift(hann(len(out))) * out
    #since even, 
    nyq = N / 2
    t_pack = np.concatenate((wout[:nyq + 1], zeros_5, wout[nyq + 1:]))

    scale_factor = len(t_pack) / N
    f_restored5 = scale_factor * fftshift(ifft(t_pack))
    w_restored5 = fftshift(fftfreq(len(t_pack), d=d)) + w_50
    print(w_restored5[10] - w_restored5[9])


    #for 10000
    N = len(f_10)
    if N % 2 == 0:
        print("Even")
    else:
        print("Odd")

    out = fft(ifftshift(f_10))
    freqs = fftfreq(N, d=0.02) # spacing, Ang
    d = freqs[1]

    zeros_10 = np.zeros((233332,))

    w_10_0 = ifftshift(w_10)[0] #must do this to get the zeroth wavelength

    wout = ifftshift(hann(len(out))) * out
    #since even, 
    nyq = N / 2
    t_pack = np.concatenate((wout[:nyq + 1], zeros_10, wout[nyq + 1:]))

    scale_factor = len(t_pack) / N
    f_restored10 = scale_factor * fftshift(ifft(t_pack))
    w_restored10 = fftshift(fftfreq(len(t_pack), d=d)) + w_10_0
    print("Match up", w_3[-1], w_restored5[0], w_restored5[-1], w_restored10[0])

    w_all = np.concatenate((w_3, w_restored5, w_restored10))
    f_all = np.concatenate((f_3, f_restored5, f_restored10))

    np.save("w_all.npy", w_all)
    np.save("f_all.npy", f_all)

    plt.plot(w_all, f_all)
    plt.plot(w, f_full, "r.")
    plt.axvline(5000)
    plt.axvline(10000)
    plt.show()


def do_sinc_interp(temp, logg):
    f_full = load_flux_full(temp, logg, True)[ind]
    global w
    intp = Sinc_w(w, f_full, a=5, window='kaiser')
    f_kms = list(map(intp, wave_grid))
    np.save("f_kms.npy", f_kms)
    plt.plot(wave_grid, f_kms)
    plt.show()

def test_integrate():
    f_full = load_flux_full(5900,3.5,"-0.0")
    #w_full
    print(trapz(f_full*1e-8, w_full))
    print(simps(f_full*1e-8, w_full))

def main():
    #Rewrite Flux
    #for temp in Ts:
    #    for logg in loggs:
    #        rewrite_flux(temp,logg)
    #write_Ts_loggs()
    #rewrite_wl()
    #load_npy(5700,4.5)
    #load_fits(5700,4.5)
    #interpolate_test_temp()
    #interpolate_test_logg()
    #write_hdf5()
    #create_grid_parallel()
    #compare_PHOENIX_TRES_spacing()
    #PHOENIX_5000(5900,3.5)
    #@do_sinc_interp(5900, 3.5)
    #create_fine_and_coarse_wave_grid()

    #return load_flux_full(5900,3.5,'-0.5',norm=True)
    create_grid_parallel(4)
    #create_grid_parallel(1)

    #load_flux_full(3400, 1.0, '+0.5', norm=True)
    #test_integrate()


if __name__ == "__main__":
    main()
