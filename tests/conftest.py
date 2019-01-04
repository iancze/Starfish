import os
from itertools import product

import numpy as np
import scipy.stats as st
import pytest

from Starfish.grid_tools import download_PHOENIX_models, Instrument, PHOENIXGridInterfaceNoAlpha, \
    HDF5Creator, HDF5Interface


@pytest.fixture(scope='session')
def grid_points():
    yield np.array(list(product(
        (6000, 6100, 6200),
        (4.0, 4.5, 5.0),
        (0.0, -0.5, -1.0)
    )))


@pytest.fixture(scope='session')
def PHOENIXModels(grid_points):
    test_base = os.path.dirname(os.path.dirname(__file__))
    outdir = os.path.join(test_base, 'data', 'phoenix')
    download_PHOENIX_models(grid_points, outdir)
    yield outdir


@pytest.fixture(scope='session')
def AlphaPHOENIXModels():
    params = [(6100, 4.5, 0.0, -0.2,)]
    test_base = os.path.dirname(os.path.dirname(__file__))
    outdir = os.path.join(test_base, 'data', 'phoenix')
    download_PHOENIX_models(params, outdir)
    yield outdir


@pytest.fixture(scope='session')
def mock_instrument():
    yield Instrument('Test instrument', FWHM=45.0, wl_range=(1e4, 4e4))


@pytest.fixture(scope='session')
def mock_no_alpha_grid(PHOENIXModels):
    yield PHOENIXGridInterfaceNoAlpha(base=PHOENIXModels)


@pytest.fixture(scope='session')
def mock_creator(mock_no_alpha_grid, mock_instrument, tmpdir_factory, grid_points):
    ranges = np.vstack([np.min(grid_points, 0), np.max(grid_points, 0)]).T
    tmpdir = tmpdir_factory.mktemp('hdf5tests')
    outfile = tmpdir.join('test_grid.hdf5')
    creator = HDF5Creator(mock_no_alpha_grid, filename=outfile, instrument=mock_instrument, wl_range=(2e4, 3e4),
                          ranges=ranges)
    yield creator


@pytest.fixture(scope='session')
def mock_hdf5(mock_creator):
    mock_creator.process_grid()
    yield mock_creator.filename

@pytest.fixture(scope='session')
def mock_hdf5_interface(mock_hdf5):
    yield HDF5Interface(mock_hdf5)

@pytest.fixture
def mock_data():
    wave = np.linspace(1e4, 5e4, 1000)
    peaks = -5 * st.norm.pdf(wave, 2e4, 200)
    peaks += -4 * st.norm.pdf(wave, 2.5e4, 80)
    peaks += -10 * st.norm.pdf(wave, 3e4, 50)
    flux = peaks + np.random.normal(0, 5, size=1000)
    yield wave, flux