import os

import numpy as np

from Starfish import Spectrum


class TestSpectrum:
    def test_reshaping(self):
        waves = [np.linspace(1e4, 2e4, 100), np.linspace(2e4, 3e4, 100)]
        fluxes = [np.sin(waves[0]), np.cos(waves[1])]
        wave = np.hstack(waves)
        flux = np.hstack(fluxes)
        data = Spectrum(wave, flux, name="single")
        assert data.shape == (1, 200)
        reshaped = data.reshape((2, -1))
        assert reshaped.name == "single"
        assert np.allclose(reshaped.waves, waves)
        assert np.allclose(reshaped.fluxes, fluxes)
        assert reshaped.shape == (2, 100)

        data.shape = (2, -1)

        assert np.allclose(reshaped.waves, data.waves)
        assert np.allclose(reshaped.fluxes, data.fluxes)

    def test_masking(self, mock_spectrum):
        mask = np.random.randn(*mock_spectrum.shape) > 0
        mock_spectrum.masks = mask
        assert np.allclose(mock_spectrum[0].wave, mock_spectrum.waves[mask])
        assert np.allclose(mock_spectrum[0].flux, mock_spectrum.fluxes[mask])
        assert np.allclose(mock_spectrum[0].sigma, mock_spectrum.sigmas[mask])

    def test_save_load(self, mock_spectrum, tmpdir):
        filename = os.path.join(tmpdir, "data.hdf5")
        mock_spectrum.save(filename)
        new_spectrum = Spectrum.load(filename)
        assert np.all(new_spectrum.waves == mock_spectrum.waves)
        assert np.all(new_spectrum.fluxes == mock_spectrum.fluxes)
        assert np.all(new_spectrum.sigmas == mock_spectrum.sigmas)
        assert np.all(new_spectrum.masks == mock_spectrum.masks)
