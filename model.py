import numpy as np
from scipy.interpolate import interp1d, LinearNDInterpolator,InterpolatedUnivariateSpline
from scipy.ndimage.filters import convolve
from scipy.special import j1
from numpy.polynomial import Chebyshev as Ch
import h5py
import yaml
import gc
from numpy.fft import fft, ifft, fftfreq, fftshift, ifftshift
#import pyfftw

f = open('config.yaml')
config = yaml.load(f)
f.close()

'''
Coding convention:
    wl: refers to an individual 1D TRES wavelength array, shape = (2304,)
    fl: refers to an individual 1D TRES flux array, shape = (2304,)

    wls: referes to 2D TRES wavelength array, shape = (51, 2304)
    fls: referes to 2D TRES flux array, shape = (51, 2304)

    wlsz: refers to 2D TRES wavelength array, shifted in velocity, shape = (51, 2304)

    w: refers to individual 1D PHOENIX wavelength array, spacing 0.01A, shape = (large number,)
    f: refers to individual 1D PHOENIX flux array, shape = (large number,)

    fls: refers to 2D model flux array, after being downsampled to TRES resolution, shape = (51, 2304)

'''

##################################################
# Constants
##################################################
c_ang = 2.99792458e18 #A s^-1
c_kms = 2.99792458e5 #km s^-1
G = 6.67259e-8 #cm3 g-1 s-2
M_sun = 1.99e33 #g
R_sun = 6.955e10 #cm
pc = 3.0856776e18 #cm
AU = 1.4959787066e13 #cm

T_points = np.array(
    [2300, 2400, 2500, 2600, 2700, 2800, 2900, 3000, 3100, 3200, 3300, 3400, 3500, 3600, 3700, 3800, 4000, 4100, 4200,
     4300, 4400, 4500, 4600, 4700, 4800, 4900, 5000, 5100, 5200, 5300, 5400, 5500, 5600, 5700, 5800, 5900, 6000, 6100,
     6200, 6300, 6400, 6500, 6600, 6700, 6800, 6900, 7000, 7200, 7400, 7600, 7800, 8000, 8200, 8400, 8600, 8800, 9000,
     9200, 9400, 9600, 9800, 10000, 10200, 10400, 10600, 10800, 11000, 11200, 11400, 11600, 11800, 12000])
logg_points = np.arange(0.0, 6.1, 0.5)
#shorten for ease of use
T_points = T_points[16:-25]
logg_points = logg_points[2:-2]

base = 'data/LkCa15//LkCa15_2013-10-13_09h37m31s_cb.flux.spec.'
wls = np.load(base + "wls.npy")
fls = np.load(base + "fls.npy")
#fls = np.load("fls_fake.npy")
sigmas = 1.2 * np.load(base + "sigma.npy")
#sigmas = np.load('sigmas_fake.npy')
masks = np.load(base + "mask.npy")
red_grid = np.load('red_grid.npy')

orders = np.array(config['orders'])
norder = len(orders)

#Truncate TRES to include only those orders
wls = wls[orders]
fls = fls[orders]
sigmas = sigmas[orders]
masks = masks[orders]

len_wl = len(wls[0])

wave_grid = np.load("wave_grid_2kms.npy")

def load_flux(temp, logg):
    fname = "HiResNpy/PHOENIX-ACES-AGSS-COND-2011/Z-0.0/lte{temp:0>5.0f}-{logg:.2f}-0.0" \
            ".PHOENIX-ACES-AGSS-COND-2011-HiRes.npy".format(
        temp=temp, logg=logg)
    #print("Loaded " + fname)
    f = np.load(fname)
    return f


def flux_interpolator_hdf5():
    #load hdf5 file of PHOENIX grid 
    fhdf5 = h5py.File('LIB_2kms.hdf5', 'r')
    LIB = fhdf5['LIB']
    param_combos = []
    var_combos = []
    for t, temp in enumerate(T_points):
        for l, logg in enumerate(logg_points):
            param_combos.append([t, l])
            var_combos.append([temp, logg])
    num_spec = len(param_combos)
    points = np.array(var_combos)
    fluxes = np.empty((num_spec, len(wave_grid)))
    for i in range(num_spec):
        t, l = param_combos[i]
        fluxes[i] = LIB[t, l]
    flux_intp = LinearNDInterpolator(points, fluxes, fill_value=1.)
    print("Loaded HDF5 interpolator")
    fhdf5.close()
    del fluxes
    gc.collect()
    return flux_intp


flux = flux_interpolator_hdf5()

##################################################
#Data processing steps
##################################################


##################################################
#Stellar Broadening
##################################################

def karray(center, width, res):
    '''Creates a kernel array with an odd number of elements, the central element centered at `center` and spanning
    out to +/- width in steps of resolution. Works similar to arange in that it may or may not get all the way to the
    edge.'''
    neg = np.arange(center - res, center - width, -res)[::-1]
    pos = np.arange(center, center + width, res)
    kar = np.concatenate([neg, pos])
    return kar


@np.vectorize
def vsini_ang(lam0, vsini, dlam=0.01, epsilon=0.6):
    '''vsini in km/s. Epsilon is the limb-darkening coefficient, typically 0.6. Formulation uses Eqn 18.14 from Gray,
    The Observation and Analysis of Stellar Photospheres, 3rd Edition.'''
    lamL = vsini * 1e13 * lam0 / c_ang
    lam = karray(0, lamL, dlam)
    c1 = 2. * (1 - epsilon) / (np.pi * lamL * (1 - epsilon / 3.))
    c2 = epsilon / (2. * lamL * (1 - epsilon / 3.))
    series = c1 * np.sqrt(1. - (lam / lamL) ** 2) + c2 * (1. - (lam / lamL) ** 2) ** 2
    return series / np.sum(series)


@np.vectorize
def G(s, vL):
    '''vL in km/s. Gray pg 475'''
    if s != 0:
        ub = 2. * np.pi * vL * s
        return j1(ub) / ub - 3 * np.cos(ub) / (2 * ub ** 2) + 3. * np.sin(ub) / (2 * ub ** 3)
    else:
        return 1.


##################################################
#Radial Velocity Shift
##################################################
@np.vectorize
def shift_vz(lam_source, vz):
    '''Given the source wavelength, lam_sounce, return the observed wavelength based upon a radial velocity vz in
    km/s. Negative velocities are towards the observer (blueshift).'''
    lam_observe = lam_source * np.sqrt((c_kms + vz) / (c_kms - vz))
    #TODO: when applied to full spectrum, this sqrt is repeated
    return lam_observe


def shift_TRES(vz):
    wlsz = shift_vz(wls, vz)
    return wlsz


##################################################
#TRES Instrument Broadening
##################################################
@np.vectorize
def gauss_kernel(dlam, V=6.8, lam0=6500.):
    '''V is the FWHM in km/s. lam0 is the central wavelength in A'''
    sigma = V / 2.355 * 1e13 #A/s
    return c_ang / lam0 * 1 / (sigma * np.sqrt(2 * np.pi)) * np.exp(- (c_ang * dlam / lam0) ** 2 / (2. * sigma ** 2))


def gauss_series(dlam, V=6.8, lam0=6500.):
    '''sampled from +/- 3sigma at dlam. V is the FWHM in km/s'''
    sigma_l = V / (2.355 * c_kms) * lam0 #A
    wl = karray(0., 6 * sigma_l, dlam)
    gk = gauss_kernel(wl)
    return gk / np.sum(gk)


##################################################
#Downsample to TRES bins 
##################################################

ones = np.ones((10,))


def downsample(w_m, f_m, w_TRES):
    out_flux = np.zeros_like(w_TRES)
    len_mod = len(w_m)

    #Determine the TRES bin edges
    len_TRES = len(w_TRES)
    edges = np.empty((len_TRES + 1,))
    difs = np.diff(w_TRES) / 2.
    edges[1:-1] = w_TRES[:-1] + difs
    edges[0] = w_TRES[0] - difs[0]
    edges[-1] = w_TRES[-1] + difs[-1]

    #Determine PHOENIX bin edges
    Pedges = np.empty((len_mod + 1,))
    Pdifs = np.diff(w_m) / 2.
    Pedges[1:-1] = w_m[:-1] + Pdifs
    Pedges[0] = w_m[0] - Pdifs[0]
    Pedges[-1] = w_m[-1] + Pdifs[-1]

    i_start = np.argwhere((edges[0] < Pedges))[0][
                  0] - 1 #return the first starting index for the model wavelength edges array (Pedges)

    edges_i = 1
    left_weight = (Pedges[i_start + 1] - edges[0]) / (Pedges[i_start + 1] - Pedges[i_start])

    for i in range(len_mod + 1):

        if Pedges[i] > edges[edges_i]:
            right_weight = (edges[edges_i] - Pedges[i - 1]) / (Pedges[i] - Pedges[i - 1])
            weights = ones[:(i - i_start)].copy()
            weights[0] = left_weight
            weights[-1] = right_weight

            out_flux[edges_i - 1] = np.average(f_m[i_start:i], weights=weights)

            edges_i += 1
            i_start = i - 1
            left_weight = 1. - right_weight
            if edges_i > len_TRES:
                break
    return out_flux


##################################################
# Model 
##################################################

def old_model(wlsz, temp, logg, vsini, flux_factor):
    '''Given parameters, return the model, exactly sliced to match the format of the echelle spectra in `efile`.
    `temp` is effective temperature of photosphere. vsini in km/s. vz is radial velocity, negative values imply
    blueshift. Assumes M, R are in solar units, and that d is in parsecs'''
    #wlsz has length norders

    #M = M * M_sun #g
    #R = R * R_sun #cm
    #d = d * pc #cm

    #logg = np.log10(G * M / R**2)
    #flux_factor = R**2/d**2 #prefactor by which to multiply model flux (at surface of star) to get recieved TRES flux

    #Loads the ENTIRE spectrum, not limited to a specific order
    f_full = flux_factor * flux(temp, logg)

    model_flux = np.zeros_like(wlsz)
    #Cycle through all the orders in the echelle spectrum
    #might be able to np.vectorize this
    for i, wlz in enumerate(wlsz):
        #print("Processing order %s" % (orders[i]+1,))

        #Limit huge file to the necessary order. Even at 4000 ang, 1 angstrom corresponds to 75 km/s. Add in an extra
        # 5 angstroms to be sure.
        ind = (w_full > (wlz[0] - 5.)) & (w_full < (wlz[-1] + 5.))
        w = w_full[ind]
        f = f_full[ind]

        #convolve with stellar broadening (sb)
        k = vsini_ang(np.mean(wlz), vsini) #stellar rotation kernel centered at order
        f_sb = convolve(f, k)

        dlam = w[1] - w[0] #spacing of model points for TRES resolution kernel

        #convolve with filter to resolution of TRES
        filt = gauss_series(dlam, lam0=np.mean(wlz))
        f_TRES = convolve(f_sb, filt)

        #downsample to TRES bins
        dsamp = downsample(w, f_TRES, wlz)
        #red = dsamp/deredden(wlz,Av,mags=False)

        #If the redenning interpolation is taking a while here, we could save the points for a given redenning and
        # simply multiply each again

        model_flux[i] = dsamp

    #Only returns the fluxes, because the wlz is actually the TRES wavelength vector
    return model_flux

#Constant for all models
ss = np.fft.fftfreq(len(wave_grid), d=2.) #2km/s spacing for wave_grid

#f_full = pyfftw.n_byte_align_empty(196608, 16, 'complex128')
#FF = pyfftw.n_byte_align_empty(196608, 16, 'complex128')
#blended = pyfftw.n_byte_align_empty(196608, 16, 'complex128')
#blended_real = pyfftw.n_byte_align_empty(196608, 16, "float64")
#fft_object = pyfftw.FFTW(f_full, FF)
#ifft_object = pyfftw.FFTW(FF, blended, direction='FFTW_BACKWARD')


def model(wlsz, temp, logg, vsini, Av, flux_factor):
    '''Given parameters, return the model, exactly sliced to match the format of the echelle spectra in `efile`.
    `temp` is effective temperature of photosphere. vsini in km/s. vz is radial velocity, negative values imply
    blueshift. Assumes M, R are in solar units, and that d is in parsecs'''
    #wlsz has length norders

    #M = M * M_sun #g
    #R = R * R_sun #cm
    #d = d * pc #cm

    #logg = np.log10(G * M / R**2)
    #flux_factor = R**2/d**2 #prefactor by which to multiply model flux (at surface of star) to get recieved TRES flux

    #Loads the ENTIRE spectrum, not limited to a specific order
    #f_full[:] = flux_factor * flux(temp, logg)
    f_full = flux_factor * flux(temp, logg)

    #Take FFT of f_grid
    FF = fft(f_full)
    #fft_object()

    ss[0] = 0.01 #junk so we don't get a divide by zero error
    ub = 2. * np.pi * vsini * ss
    sb = j1(ub) / ub - 3 * np.cos(ub) / (2 * ub ** 2) + 3. * np.sin(ub) / (2 * ub ** 3)
    #set zeroth frequency to 1 separately (DC term)
    sb[0] = 1.

    #FF[:] *= sb #institute velocity taper
    FF *= sb

    #do ifft
    #ifft_object()
    blended_real = np.abs(ifft(FF))

    #blended_real[:] = np.absolute(blended) #remove tiny complex component

    #redden spectrum
    red = blended_real / 10**(0.4 * Av * red_grid)

    #do synthetic photometry to compare to points

    f = InterpolatedUnivariateSpline(wave_grid, red)
    fresult = f(wlsz.flatten()) #do spline interpolation to TRES pixels
    result = np.reshape(fresult,(norder,-1))
    del f
    gc.collect() #necessary to prevent memory leak!
    return result


def degrade_flux(wl, w, f_full):
    vsini = 40.
    #Limit huge file to the necessary order. Even at 4000 ang, 1 angstrom corresponds to 75 km/s. Add in an extra 5
    # angstroms to be sure.
    ind = (w_full > (wl[0] - 5.)) & (w_full < (wl[-1] + 5.))
    w = w_full[ind]
    f = f_full[ind]
    #convolve with stellar broadening (sb)
    k = vsini_ang(np.mean(wl), vsini) #stellar rotation kernel centered at order
    f_sb = convolve(f, k)

    dlam = w[1] - w[0] #spacing of model points for TRES resolution kernel

    #convolve with filter to resolution of TRES
    filt = gauss_series(dlam, lam0=np.mean(wl))
    f_TRES = convolve(f_sb, filt)

    #downsample to TRES bins
    dsamp = downsample(w, f_TRES, wl)

    return dsamp


def data(coefs_arr, wls, fls):
    '''coeff is a (norders, npoly) shape array'''
    flsc = np.zeros_like(fls)
    for i, coefs in enumerate(coefs_arr):
        #do this to keep constant fixed at 1
        flsc[i] = Ch(np.append([1],coefs),domain=[wls[i][0],wls[i][-1]])(wls[i]) * fls[i]
        #do this to allow tweaks to each order
        #flsc[i] = Ch(coefs, domain=[wls[i][0], wls[i][-1]])(wls[i]) * fls[i]
    return flsc

xs = np.arange(len_wl)
T0 = np.ones_like(xs)
Ch1 = Ch([0,1], domain=[0,len_wl-1])
T1 = Ch1(xs)
Ch2 = Ch([0,0,1],domain=[0,len_wl-1])
T2 = Ch2(xs)
Ch3 = Ch([0,0,0,1],domain=[0,len_wl-1])
T3 = Ch3(xs)
T = np.array([T0,T1,T2,T3]) #multiply this by the flux and sigma vector for each order
TT = np.einsum("in,jn->ijn",T,T)
mu = np.array([1,0,0,0])
sigmac = 0.2
D = sigmac**(-2) * np.eye(4)
Dmu = np.einsum("ij,j->j",D,mu)
muDmu = np.einsum("j,j->",mu,Dmu)

def lnprob(p):
    '''New lnprob, no nuisance coeffs'''
    temp, logg, vsini, vz, Av, flux_factor = p
    if (logg < 0) or (logg > 6.0) or (vsini < 0) or (temp < 2300) or (temp > 10000) or (Av < 0):
        return -np.inf
    else:
        #shift TRES wavelengths
        wlsz = wls * np.sqrt((c_kms + vz) / (c_kms - vz))
        fmods = model(wlsz, temp, logg, vsini, Av, flux_factor)

        a= fmods**2/sigmas**2
        A = np.einsum("in,jkn->ijk",a,TT)
        Ap = A + D
        detA = np.array(list(map(np.linalg.det, Ap)))
        invA = np.array(list(map(np.linalg.inv, Ap)))

        b = fmods * fls / sigmas**2
        B = np.einsum("in,jn->ij",b,T)
        Bp = B + Dmu

        g = -0.5 * fls**2/sigmas**2
        G = np.einsum("ij->i",g)
        Gp = G - 0.5 * muDmu

        invAB = np.einsum("ijk,ik->ij",invA,Bp)
        BAB = np.einsum("ij,ij->i",Bp,invAB)

        lnp = np.sum(0.5 * np.log((2. * np.pi)**norder/detA) + 0.5 * BAB + Gp)

        return lnp


def lnprob_old(p):
    '''p is the parameter vector, contains both theta_s and theta_n'''
    #print(p)
    temp, logg, vsini, vz, flux_factor = p[:5]
    if (logg < 0) or (logg > 6.0) or (vsini < 0) or (temp < 2300) or (temp > 10000): #or (Av < 0):
        return -np.inf
    else:
        coefs = p[5:]
        #print(coefs)
        coefs_arr = coefs.reshape(len(orders), -1)

        #shift TRES wavelengths
        wlsz = wls * np.sqrt((c_kms + vz) / (c_kms - vz))

        flsc = data(coefs_arr, wlsz, fls)

        fs = model(wlsz, temp, logg, vsini, flux_factor)

        chi2 = np.sum(((flsc - fs) / sigmas) ** 2)
        L = -0.5 * chi2
        #prior = - np.sum((coefs_arr[:,2])**2/0.1) - np.sum((coefs_arr[:,[1,3,4]]**2/0.01))
        prior = 0
        return L + prior


def model_and_data(p):
    '''p is the parameter vector, contains both theta_s and theta_n'''
    #print(p)
    temp, logg, vsini, vz, Av, flux_factor = p[:6]
    coefs = p[6:]
    print(coefs)
    coefs_arr = coefs.reshape(len(orders), -1)

    wlsz = shift_TRES(vz)

    flsc = data(coefs_arr, wlsz, fls)

    fs = model(wlsz, temp, logg, vsini, Av, flux_factor)
    return [wlsz, flsc, fs]

def generate_fake_data(temp, logg, vsini, vz, Av, flux_factor):
    '''Generate an echelle-like spectrum to test method'''
    wlsz = shift_TRES(vz)
    fls_fake = model(wlsz, temp, logg, vsini, Av, flux_factor)
    avg = np.percentile(fls_fake, 50)
    print(avg)

    #func = lambda x: np.random.normal(loc=0,scale=x)
    #noise = np.array(list(map(func,sigmas)))
    noise = np.random.normal(loc=0, scale=(avg/25.),size=fls_fake.shape)
    fls_fake += noise
    np.save('fls_fake.npy',fls_fake)
    np.save('sigmas_fake.npy',noise)
    pass

def main():
    #print(model(wls,7005,6.1,40,1e-27))
    #F = 8e-28
    #print(lnprob(np.array([5905, 3.5, 40, 27,0.83 * F])))
    #print(lnprob(np.array([6000, 3.5, 40, 100, 1.0, F])))

    #generate_fake_data(6000,3.5, 15, 15, 1.0, 1e-27)
    print(lnprob(np.array([6000,3.5,15,15,1.0,1e-27])))

    pass


if __name__ == "__main__":
    main()
