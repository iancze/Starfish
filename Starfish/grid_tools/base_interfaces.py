import itertools
import logging
import multiprocessing as mp
import os

import h5py
import numpy as np
from astropy.io import fits
from tqdm import tqdm

import Starfish.constants as C
from Starfish import config
from Starfish.models.transforms import instrumental_broaden, resample
from Starfish.utils import calculate_dv, calculate_dv_dict, create_log_lam_grid
from .utils import chunk_list

log = logging.getLogger(__name__)


class RawGridInterface:
    """
    A base class to handle interfacing with synthetic spectral libraries.

    :param name: name of the spectral library
    :type name: str
    :param param_names: the names of the parameters (dimensions) of the grid
    :type param_names: list
    :param points: the grid points at which
        spectra exist (assumes grid is square, not ragged, meaning that every combination
        of parameters specified exists in the grid).
    :type points: list of numpy arrays
    :param wl_range: the starting and ending wavelength ranges of the grid to
        truncate to. If None, will use whole available grid. Default is None.
    :type wl_range: list of len 2 [min, max]
    :param air: Are the wavelengths measured in air?
    :type air: bool
    :param base: path to the root of the files on disk.
    :type base: str
    """

    def __init__(self, name, param_names, points, wl_range=None, air=True, base=None):
        self.name = name
        self.param_names = param_names
        self.points = points
        self.air = air
        self.wl_range = wl_range
        self.base = base

    def check_params(self, parameters):
        """
        Determine if the specified parameters are allowed in the grid.

        :param parameters: parameter set to check
        :type parameters: numpy.ndarray or list

        :raises ValueError: if the parameter values are outside of the grid bounds

        :returns: True if found in grid
        """
        if not isinstance(parameters, np.ndarray):
            parameters = np.array(parameters)

        if len(parameters) != len(self.param_names):
            raise ValueError("Length of given parameters ({}) does not match length of grid parameters ({})".format(
                len(parameters), len(self.param_names)))

        for param, params in zip(parameters, self.points):
            if param not in params:
                raise ValueError("{} not in the grid points {}".format(param, params))
        return True

    def load_flux(self, parameters, header=False, norm=True):
        """
        Load the flux and header information.

       :param parameters: stellar parameters
       :type parameters: numpy.ndarray or list
       :param header: If True, will return the header alongside the flux. Default is False.
       :type header: bool
       :param norm: If True, will normalize the flux to solar luminosity. Default is True.
       :type norm: bool

       :raises ValueError: if the file cannot be found on disk.

       :returns: numpy.ndarray if header is False, tuple of (numpy.ndarray, dict) if header is True
       """
        raise NotImplementedError("`load_flux` is abstract and must be implemented by subclasses")


class HDF5Creator:
    """
    Create a HDF5 grid to store all of the spectra from a RawGridInterface,
    along with metadata.

    :param GridInterface:  The raw grid interface to process while creating the HDF5 file
    :type GridInterface: :obj:`RawGridInterface`
    :param instrument: If provided, the instrument to convolve/truncate the grid. If None, will
        maintain the grid's original wavelengths and resolution. Default is None
    :type instrument: :obj:`instrument`
    :param filename: where to create the HDF5 file. Suffix ``*.hdf5`` recommended. Default is
        ``Starfish.config.grid['hdf5_path']``
    :type filename: str or path-like
    :param wl_range: The wavelength range to truncate the grid to. Will be truncated to match grid wavelengths
         and instrument wavelengths if over or under specified. Default is ``Starfish.config.grid['wl_range']``. If set
         to None, will not truncate grid.
    :type wl_range: length 2 iterable of (min, max)
    :param ranges: lower and upper limits for each stellar parameter,
        in order to truncate the number of spectra in the grid. If None, will not restrict the
        range of the parameters. Default is ``Starfish.config.grid['parrange']``.
    :type ranges: iterable of length-2 iterables (min, max)
    :param key_name: formatting string that has keys for each of the parameter
        names to translate into a hash-able string. Default is ``Starfish.config.grid['key_name']``
    :type key_name: str

    :raises: ValueError if the wl_range is ill-specified or if the parameter range are completely disjoint from the
        grid points.
    """

    def __init__(self, GridInterface, instrument=None, filename=config.grid['hdf5_path'],
                 wl_range=config.grid['wl_range'], ranges=config.grid['parrange'],
                 key_name=config.grid['key_name']):

        self.log = logging.getLogger(self.__class__.__name__)

        self.GridInterface = GridInterface
        self.filename = os.path.expandvars(filename)
        self.instrument = instrument

        # The flux formatting key will always have alpha in the name, regardless
        # of whether or not the library uses it as a parameter.
        self.key_name = key_name

        if ranges is None:
            self.points = self.GridInterface.points
        else:
            # Take only those points of the GridInterface that fall within the ranges specified
            self.points = []
            # We know which subset we want, so use these.
            for i, (low, high) in enumerate(ranges):
                valid_points = self.GridInterface.points[i]
                ind = (valid_points >= low) & (valid_points <= high)
                self.points.append(valid_points[ind])
                # Note that at this point, this is just the grid points that fall within the rectangular
                # bounds set by ranges. If the raw library is actually irregular (e.g. CIFIST),
                # then self.points will contain points that don't actually exist in the raw library.

        # the raw wl from the spectral library
        self.wl_native = self.GridInterface.wl  # raw grid
        self.dv_native = calculate_dv(self.wl_native)

        self.hdf5 = h5py.File(self.filename, "w")
        self.hdf5.attrs["grid_name"] = GridInterface.name
        self.hdf5.flux_group = self.hdf5.create_group("flux")
        self.hdf5.flux_group.attrs["unit"] = "erg/cm^2/s/A"

        # We'll need a few wavelength grids
        # 1. The original synthetic grid: ``self.wl_native``
        # 2. A finely spaced log-lambda grid respecting the ``dv`` of
        #   ``self.wl_native``, onto which we can interpolate the flux values
        #   in preperation of the FFT: ``self.wl_loglam``
        # [ DO FFT ]
        # 3. A log-lambda spaced grid onto which we can downsample the result
        #   of the FFT, spaced with a ``dv`` such that we respect the remaining
        #   Fourier modes: ``self.wl_final``

        # There are three ranges to consider when wanting to make a grid:
        # 1. The full range of the synthetic library
        # 2. The full range of the instrument/dataset
        # 3. The range specified by the user in config.yaml
        # For speed reasons, we will always truncate to to wl_range. If either
        # the synthetic library or the instrument library is smaller than this range,
        # raise an error.
        if wl_range is None:
            wl_min, wl_max = 0, np.inf
        else:
            wl_min, wl_max = wl_range
        buffer = config.grid["buffer"]  # [AA]
        wl_min -= buffer
        wl_max += buffer

        # If the raw synthetic grid doesn't span the full range of the user
        # specified grid, raise an error.
        # Instead, let's choose the maximum limit of the synthetic grid?
        if self.instrument is not None:
            inst_min, inst_max = self.instrument.wl_range
        else:
            inst_min, inst_max = -np.inf, np.inf
        imposed_min = np.max([self.wl_native[0], inst_min])
        imposed_max = np.min([self.wl_native[-1], inst_max])
        if wl_min < imposed_min:
            self.log.info('Given minimum wavelength ({}) is less than instrument or grid minimum. Truncating to {}'
                          .format(wl_min, imposed_min))
            wl_min = imposed_min
        if wl_max > imposed_max:
            self.log.info('Given maximum wavelength ({}) is greater than instrument or grid maximum. Truncating to {}'
                          .format(wl_max, imposed_max))
            wl_max = imposed_max

        if wl_max < wl_min:
            raise ValueError('Minimum wavelength must be less than maximum wavelength')

        # Calculate wl_loglam
        # use the dv that preserves the native quality of the raw PHOENIX grid
        wl_dict = create_log_lam_grid(self.dv_native, wl_min, wl_max)
        wl_loglam = wl_dict["wl"]
        dv_loglam = calculate_dv_dict(wl_dict)

        self.log.info("FFT grid stretches from {} to {}".format(wl_loglam[0], wl_loglam[-1]))
        self.log.info("wl_loglam dv is {} km/s".format(dv_loglam))

        if self.instrument is None:
            mask = (self.wl_native > wl_min) & (self.wl_native < wl_max)
            self.wl_final = self.wl_native[mask]
            self.dv_final = self.dv_native
            inst_broaden = lambda w, f: (w, f)
        else:
            inst_broaden = lambda w, f: (w, instrumental_broaden(w, f, self.instrument.FWHM))
            # The final wavelength grid, onto which we will interpolate the
            # Fourier filtered wavelengths, is part of the instrument object
            dv_temp = self.instrument.FWHM / self.instrument.oversampling
            wl_dict = create_log_lam_grid(dv_temp, wl_min, wl_max)
            self.wl_final = wl_dict["wl"]
            self.dv_final = calculate_dv_dict(wl_dict)

        resample_loglam = lambda w, f: (wl_loglam, resample(w, f, wl_loglam))
        resample_final = lambda w, f: (self.wl_final, resample(w, f, self.wl_final))
        self.transform = lambda flux: resample_final(*inst_broaden(*resample_loglam(self.wl_native, flux)))

        # Create the wl dataset separately using float64 due to rounding errors w/ interpolation.
        wl_dset = self.hdf5.create_dataset("wl", data=self.wl_final, compression='gzip',
                                           compression_opts=9)
        wl_dset.attrs["air"] = self.GridInterface.air
        wl_dset.attrs["dv"] = self.dv_final

    def process_grid(self):
        """
        Run :meth:`process_flux` for all of the spectra within the `ranges`
        and store the processed spectra in the HDF5 file.
        """

        # points is now a list of numpy arrays of the values in the grid
        # Take all parameter permutations in self.points and create a list
        # param_list will be a list of numpy arrays, specifying the parameters
        param_list = []

        # use itertools.product to create permutations of all possible values
        for i in itertools.product(*self.points):
            param_list.append(np.array(i))

        all_params = np.array(param_list)

        invalid_params = []

        self.log.debug("Total of {} files to process.".format(len(param_list)))

        pbar = tqdm(all_params)
        for i, param in enumerate(pbar):
            pbar.set_description("Processing {}".format(param))
            # Load and process the flux
            try:
                flux, header = self.GridInterface.load_flux(param, header=True)
            except ValueError:
                self.log.warning("Deleting {} from all params, does not exist.".format(param))
                invalid_params.append(i)
                continue

            _, fl_final = self.transform(flux)

            flux = self.hdf5["flux"].create_dataset(self.key_name.format(*param),
                                                    data=fl_final, compression='gzip',
                                                    compression_opts=9)
            # Store header keywords as attributes in HDF5 file
            for key, value in header.items():
                if key != "" and value != "":  # check for empty FITS kws
                    flux.attrs[key] = value

        # Remove parameters that do no exist
        all_params = np.delete(all_params, invalid_params, axis=0)

        self.hdf5.create_dataset("pars", data=all_params, compression='gzip',
                                 compression_opts=9)

        self.hdf5.close()


class HDF5Interface:
    """
    Connect to an HDF5 file that stores spectra.

    :param filename: the name of the HDF5 file
    :type param: string
    :param ranges: optionally select a smaller part of the grid to use.
    :type ranges: dict
    """

    def __init__(self, filename=config.grid["hdf5_path"], key_name=config.grid["key_name"]):
        self.filename = os.path.expandvars(filename)
        self.key_name = key_name

        # In order to properly interface with the HDF5 file, we need to learn
        # a few things about it

        # 1.) Which parameter combinations exist in the file (self.grid_points)
        # 2.) What are the minimum and maximum values for each parameter (self.bounds)
        # 3.) Which values exist for each parameter (self.points)

        with h5py.File(self.filename, "r") as hdf5:
            self.wl = hdf5["wl"][:]
            self.wl_header = dict(hdf5["wl"].attrs.items())
            self.dv = self.wl_header["dv"]
            self.grid_points = hdf5["pars"][:]

        # determine the bounding regions of the grid by sorting the grid_points
        low = np.min(self.grid_points, axis=0)
        high = np.max(self.grid_points, axis=0)
        self.bounds = np.vstack((low, high)).T
        self.points = [np.unique(self.grid_points[:, i]) for i in range(self.grid_points.shape[1])]

        self.ind = None  # Overwritten by other methods using this as part of a ModelInterpolator

        # Test if key-name is specified correctly
        try:
            self.load_flux(self.grid_points[0])
        except (IndexError, KeyError):
            raise ValueError("key_name is ill-specified.")

    def load_flux(self, parameters, header=False):
        """
        Load just the flux from the grid, with possibly an index truncation.

        :param parameters: the stellar parameters
        :type parameters: iterable
        :param header: If True, will return the header as well as the flux. Default is False
        :type header: bool

        :raises GridError: if spectrum is not found in the HDF5 file.

        :returns: numpy.ndarray if header is False, otherwise (numpy.ndarray, dict)
        """
        if not isinstance(parameters, np.ndarray):
            parameters = np.array(parameters)

        key = self.key_name.format(*parameters)
        with h5py.File(self.filename, "r") as hdf5:
            hdr = dict(hdf5['flux'][key].attrs)
            if self.ind is not None:
                fl = hdf5['flux'][key][self.ind[0]:self.ind[1]]
            else:
                fl = hdf5['flux'][key][:]

        # Note: will raise a KeyError if the file is not found.
        if header:
            return fl, hdr
        else:
            return fl

    @property
    def fluxes(self):
        """
        Iterator to loop over all of the spectra stored in the grid, for PCA.
        Loops over parameters in the order specified by grid_points.

        :returns: generator of either numpy.ndarray
        """

        for grid_point in self.grid_points:
            yield self.load_flux(grid_point, header=False)


def create_fits(filename, fl, CRVAL1, CDELT1, dict=None):
    """Assumes that wl is already log lambda spaced"""

    hdu = fits.PrimaryHDU(fl)
    head = hdu.header
    head["DISPTYPE"] = 'log lambda'
    head["DISPUNIT"] = 'log angstroms'
    head["CRPIX1"] = 1.

    head["CRVAL1"] = CRVAL1
    head["CDELT1"] = CDELT1
    head["DC-FLAG"] = 1

    if dict is not None:
        for key, value in dict.items():
            head[key] = value

    hdu.writeto(filename)


class MasterToFITSIndividual:
    """
    Object used to create one FITS file at a time.

    :param interpolator: The interpolator of the master grid.
    :type interpolator: :class:`Interpolator`
    :param instrument: The instrument corresponding to the final spectra
    :type instrument: :class:`Instrument`
    """

    def __init__(self, interpolator, instrument):
        self.interpolator = interpolator
        self.instrument = instrument
        self.filename = "t{temp:0>5.0f}g{logg:0>2.0f}{Z_flag}{Z:0>2.0f}v{vsini:0>3.0f}.fits"

        # Create a master wl_dict which correctly oversamples the instrumental kernel
        self.wl_dict = self.instrument.wl_dict
        self.wl = self.wl_dict["wl"]

    def process_spectrum(self, parameters, out_unit, out_dir=""):
        """
        Creates a FITS file with given parameters


        :param parameters: stellar parameters :attr:`temp`, :attr:`logg`, :attr:`Z`, :attr:`vsini`
        :type parameters: dict
        :param out_unit: output flux unit? Choices between `f_lam`, `f_nu`, `f_nu_log`, or `counts/pix`. `counts/pix` will do spline integration.
        :param out_dir: optional directory to prepend to output filename, which is chosen automatically for parameter values.

        Smoothly handles the *C.InterpolationError* if parameters cannot be interpolated from the grid and prints a message.
        """

        # Preserve the "popping of parameters"
        parameters = parameters.copy()

        # Load the correct C.grid_set value from the interpolator into a LogLambdaSpectrum
        if parameters["Z"] < 0:
            zflag = "m"
        else:
            zflag = "p"

        filename = out_dir + self.filename.format(temp=parameters["temp"], logg=10 * parameters["logg"],
                                                  Z=np.abs(10 * parameters["Z"]), Z_flag=zflag,
                                                  vsini=parameters["vsini"])
        vsini = parameters.pop("vsini")
        try:
            spec = self.interpolator(parameters)
            # Using the ``out_unit``, determine if we should also integrate while doing the downsampling
            if out_unit == "counts/pix":
                integrate = True
            else:
                integrate = False
            # Downsample the spectrum to the instrumental resolution.
            spec.instrument_and_stellar_convolve(self.instrument, vsini, integrate)
            spec.write_to_FITS(out_unit, filename)
        except C.InterpolationError as e:
            print("{} cannot be interpolated from the grid.".format(parameters))

        print("Processed spectrum {}".format(parameters))


class MasterToFITSGridProcessor:
    """
    Create one or many FITS files from a master HDF5 grid. Assume that we are not going to need to interpolate
    any values.

    :param interface: The master grid.
    :type interface: :obj:`HDF5Interface`
    :param points: lists of output parameters (assumes regular grid)
    :type points: dict of lists
    :param flux_unit: format of output spectra {"f_lam", "f_nu", "ADU"}
    :type flux_unit: str
    :param outdir: output directory
    :type outdir: str or path-like
    :param processes: how many processors to use in parallel
    :type processes: int

    Basically, this object is doing a one-to-one conversion of the PHOENIX spectra. No interpolation necessary,
    preserving all of the header keywords.
    """

    def __init__(self, interface, instrument, points, flux_unit, outdir, alpha=False, integrate=False,
                 processes=mp.cpu_count()):
        self.interface = interface
        self.instrument = instrument
        self.points = points  # points is a dictionary with which values to spit out for each parameter
        self.filename = "t{temp:0>5.0f}g{logg:0>2.0f}{Z_flag}{Z:0>2.0f}v{vsini:0>3.0f}.fits"
        self.flux_unit = flux_unit
        self.integrate = integrate
        self.outdir = outdir
        self.processes = processes
        self.pids = []
        self.alpha = alpha

        self.vsini_points = self.points.pop("vsini")
        names = self.points.keys()

        # Creates a list of parameter dictionaries [{"temp":8500, "logg":3.5, "Z":0.0}, {"temp":8250, etc...}, etc...]
        # which does not contain vsini
        self.param_list = [dict(zip(names, params)) for params in itertools.product(*self.points.values())]

        # Create a master wl_dict which correctly oversamples the instrumental kernel
        self.wl_dict = self.instrument.wl_dict
        self.wl = self.wl_dict["wl"]

        # Check that temp, logg, Z are within the bounds of the interface
        for key, value in self.points.items():
            min_val, max_val = self.interface.bounds[key]
            assert np.min(self.points[key]) >= min_val, "Points below interface bound {}={}".format(key, min_val)
            assert np.max(self.points[key]) <= max_val, "Points above interface bound {}={}".format(key, max_val)

        # Create a temporary grid to resample to that matches the bounds of the instrument.
        low, high = self.instrument.wl_range
        self.temp_grid = create_log_lam_grid(wl_start=low, wl_end=high, min_vc=0.1)['wl']

    def process_spectrum_vsini(self, parameters):
        """
        Create a set of FITS files with given stellar parameters temp, logg, Z and all combinations of `vsini`.

        :param parameters: stellar parameters
        :type parameters: dict

        Smoothly handles the *KeyError* if parameters cannot be drawn from the interface and prints a message.
        """

        try:
            # Check to see if alpha, otherwise append alpha=0 to the parameter list.
            if not self.alpha:
                parameters.update({"alpha": 0.0})
            print(parameters)

            if parameters["Z"] < 0:
                zflag = "m"
            else:
                zflag = "p"

            # This is a Base1DSpectrum
            base_spec = self.interface.load_file(parameters)

            master_spec = base_spec.to_LogLambda(instrument=self.instrument,
                                                 min_vc=0.1 / C.c_kms)  # convert the Base1DSpectrum to a LogLamSpectrum

            # Now process the spectrum for all values of vsini

            for vsini in self.vsini_points:
                spec = master_spec.copy()
                # Downsample the spectrum to the instrumental resolution, integrate to give counts/pixel
                spec.instrument_and_stellar_convolve(self.instrument, vsini, integrate=self.integrate)

                # Update spectrum with vsini
                spec.metadata.update({"vsini": vsini})
                filename = self.outdir + self.filename.format(temp=parameters["temp"], logg=10 * parameters["logg"],
                                                              Z=np.abs(10 * parameters["Z"]), Z_flag=zflag,
                                                              vsini=vsini)

                spec.write_to_FITS(self.flux_unit, filename)

        except KeyError as e:
            print("{} cannot be loaded from the interface.".format(parameters))

    def process_chunk(self, chunk):
        """
        Process a chunk of parameters to FITS

        :param chunk: stellar parameter dicts
        :type chunk: 1-D list
        """
        print("Process {} processing chunk {}".format(os.getpid(), chunk))
        for param in chunk:
            self.process_spectrum_vsini(param)

    def process_all(self):
        """
        Process all parameters in :attr:`points` to FITS by chopping them into chunks.
        """
        print("Total of {} FITS files to create.".format(len(self.vsini_points) * len(self.param_list)))
        chunks = chunk_list(self.param_list, n=self.processes)
        for chunk in chunks:
            p = mp.Process(target=self.process_chunk, args=(chunk,))
            p.start()
            self.pids.append(p)

        for p in self.pids:
            # Make sure all threads have finished
            p.join()