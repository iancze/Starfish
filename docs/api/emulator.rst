=================
Spectral Emulator
=================

.. py:module:: Starfish.emulator
   :synopsis: Return a probability distribution over possible interpolated spectra.

The spectral emulator can be likened to the engine behind *Starfish*. While the novelty of *Starfish* comes from using Gaussian processes to model and account for the covariances of spectral fits, we still need a way to produce model spectra by interpolating from our synthetic library. While we could interpolate spectra from the synthetic library using something like linear interpolation in each of the library parameters, it turns out that high signal-to-noise data requires something more sophisticated. This is because the error in any interpolation can constitute a significant portion of the error budget. This means that there is a chance that non-interpolated spectra (e.g., the parameters of the synthetic spectra in the library) might be given preference over any other interpolated spectra, and the posteriors will be peaked at the grid point locations. Because the spectral emulator returns a probability distribution over possible interpolated spectra, this interpolation error can be quantified and propagated forward into the likelihood calculation.

Eigenspectra decomposition
==========================

The first step of configuring the spectral emulator is to choose a subregion of the spectral library corresponding to the star that you will fit. Then, we want to decompose the information content in this subset of the spectral library into several *eigenspectra*. [Figure A.1 here].

The eigenspectra decomposition is performed via Principal Component Analysis (PCA). Thankfully, most of the heavy lifting is already implemented by the ``scipy`` package.

:class:`PCAGrid` implements the functionality to create the eigenspectra grid from a synthetic library, and then later
query eigenspectra from it.

For example, the following takes a bunch of spectra specified by an :class:`HDF5Interface` and decomposes them into a
PCA basis

.. code-block:: python

    from Starfish.grid_tools import HDF5Interface
    from Starfish.emulator import PCAGrid

    # Load the HDF5 interface using the values in config.yaml
    myHDF5 = HDF5Interface()

    pca = PCAGrid.create(myHDF5)
    pca.write()


Optimizing the emulator
=======================

Once the synthetic library is decomposed into a set of eigenspectra, the next step is to train the Gaussian Processes (GP) that will serve as interpolators. For more explanation about the choice of Gaussian Process covariance functions and the design of the emulator, see the appendix of our paper.

The optimization of the GP hyperparameters can be carried out using either a minimization method or an MCMC sampler.
 We provide functionality based on both ``scipy.optimize.minimize`` and ``emcee``

To optimize the code, we will use the :func:`PCAGrid.optimize` routine.

Example optimizing using minimization optimizer

.. code-block:: python

    from Starfish.emulator import PCAGrid

    # Assuming you have already generated the initial PCA file
    pca = PCAGrid.open()
    pca.optimize()

Example using the ``emcee`` optimizer

.. code-block:: python

    from Starfish.emulator import PCAGrid

    # Assuming you have already generated the inital PCA file
    pca = PCAGrid.open()
    pca.optimize(method='emcee')


You can stop and resume  the optimizer if you are using the ``emcee`` method due to the convenient HDF5 backend
available in ``emcee>=3.0``. Samplers will automatically resume from their previous state unless told not to. To
restart the sampler either delete the ``emcee_progress.hdf5`` file that was produced or issue

.. code-block:: python

    pca.optimize(method='emcee', resume=False)


.. warning::
    This optimization may take a very long time to run (multiple hours). We recommend running the code on a server
    and running it in the background. For each PCAGrid you only have to optimize once, thankfully.

Once optimized, the optimal parameters will be written into the HDF5 file that contains the
PCA grid. (Located in ``config.yaml`` as ``PCA["path"]``). They are stored under the ``eparams`` key.

Model spectrum reconstruction
=============================

Once the PCAGrid has been optimized, we can finally use the emulator as a means of interpolating spectra. Using the
emulator is very similar to using a :class:`GridInterface`.

.. code-block:: python

    from Starfish.emulator import Emulator

    emu = Emulator.open()
    flux = emu.load_flux([7054, 4.0324, 0.01])
    wl = emu.wl


Utility Plotting Methods
========================

.. autofunction:: plot_reconstructed
.. autofunction:: plot_eigenspectra
.. autofunction:: plot_priors
.. autofunction:: plot_corner
.. autofunction:: plot_emulator


Reference
=========

Emulator
--------

.. autoclass:: Emulator
    :members:

PCAGrid
-------

.. autoclass:: PCAGrid
    :members: